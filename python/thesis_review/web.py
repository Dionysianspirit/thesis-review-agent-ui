from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from socket import gaierror
from urllib.parse import quote_plus, urlsplit

import httpx

from thesis_review.errors import ReviewError

# Fallback chain inside the existing controlled-search frame: if one HTML
# endpoint is down or blocks us, the next one is tried before giving up.
SEARCH_ENDPOINTS = (
    "https://html.duckduckgo.com/html/",
    "https://lite.duckduckgo.com/lite/",
)
USER_AGENT = "ThesisReviewAgent/0.7 (+local-teacher-review)"
RESULT_RE = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
LITE_LINK_RE = re.compile(
    r'<a[^>]*class="[^"]*result-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
SNIPPET_RE = re.compile(r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
PRIVATE_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback", "metadata"})


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    source_type: str = "web"
    query: str = ""
    checked_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def validate_public_url(url: str) -> str:
    """Reject loopback/private/link-local targets: the review helper only ever
    needs the public web, and the agent must not be able to point it at the
    teacher's own machine or cloud metadata endpoints."""
    target = (url or "").strip()
    parts = urlsplit(target)
    if parts.scheme not in {"http", "https"}:
        raise ReviewError("invalid_params", "外部来源只允许 http/https 地址。")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ReviewError("invalid_params", "外部来源地址无效。")
    if host in PRIVATE_HOSTNAMES or host.endswith(".local") or host.endswith(".internal"):
        raise ReviewError("invalid_params", "拒绝访问本机或内网地址。")
    if _is_forbidden_ip_literal(host):
        raise ReviewError("invalid_params", "拒绝访问本机或内网地址。")
    try:
        resolved = {info[4][0] for info in _resolve_all(host)}
    except gaierror:
        return target  # DNS failure surfaces later as search_failed
    for ip_text in resolved:
        if _ip_is_forbidden(ip_text):
            raise ReviewError("invalid_params", "拒绝访问本机或内网地址。")
    return target


def _resolve_all(host: str):
    import socket

    return socket.getaddrinfo(host, None)


def _is_forbidden_ip_literal(host: str) -> bool:
    try:
        return _ip_is_forbidden(ipaddress.ip_address(host))
    except ValueError:
        return False


def _ip_is_forbidden(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class WebSearcher:
    """Provider-independent HTML search. Failures return errors, never fabricated hits."""

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 15.0) -> None:
        self._client = client
        self.timeout = timeout

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        needle = (query or "").strip()
        if not needle:
            raise ReviewError("invalid_params", "缺少检索词。")
        errors: list[str] = []
        for endpoint in SEARCH_ENDPOINTS:
            try:
                html = self._get_text(f"{endpoint}?q={quote_plus(needle)}")
            except ReviewError as exc:
                errors.append(f"{endpoint}: {exc.message}")
                continue
            hits = _parse_results(html, query=needle, limit=limit)
            if hits:
                return hits
            # An endpoint that answers with zero parses is not fatal; the
            # fallback may simply serve a different layout.
        if errors:
            raise ReviewError("search_failed", f"外部检索失败，未写入结果。{'；'.join(errors)}")
        return []

    def fetch(self, url: str, *, max_chars: int = 4000) -> dict:
        target = (url or "").strip()
        target = validate_public_url(target)
        text = self._get_text(target)
        plain = _strip_tags(text)
        return {
            "ok": True,
            "url": target,
            "title": _guess_title(text),
            "text": plain[:max_chars],
            "truncated": len(plain) > max_chars,
            "checked_time": _now(),
        }

    def _get_text(self, url: str) -> str:
        try:
            if self._client is not None:
                response = self._client.get(url, timeout=self.timeout, headers={"User-Agent": USER_AGENT})
            else:
                response = httpx.get(url, timeout=self.timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - network boundary
            raise ReviewError("search_failed", f"外部检索失败，未写入结果。{exc}") from exc
        return response.text or ""


def _parse_results(html: str, *, query: str, limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    snippets = [_strip_tags(item) for item in SNIPPET_RE.findall(html)]
    pairs = RESULT_RE.findall(html) or LITE_LINK_RE.findall(html)
    checked = _now()
    for index, (href, title_html) in enumerate(pairs):
        url = unescape(href).strip()
        if not url.startswith("http"):
            continue
        try:
            url = validate_public_url(url)
        except ReviewError:
            continue
        title = _strip_tags(title_html) or url
        snippet = snippets[index] if index < len(snippets) else ""
        hits.append(
            SearchHit(
                title=title,
                url=url,
                snippet=snippet,
                source_type="web",
                query=query,
                checked_time=checked,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _strip_tags(html: str) -> str:
    text = unescape(TAG_RE.sub(" ", html or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _guess_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html or "", re.I | re.S)
    if not match:
        return ""
    return _strip_tags(match.group(1)).strip()[:180]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
