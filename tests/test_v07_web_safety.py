"""V0.7 web search safety: refuse loopback/private targets, fall back endpoints."""
from __future__ import annotations

import httpx
import pytest

from thesis_review.errors import ReviewError
from thesis_review.web import SEARCH_ENDPOINTS, WebSearcher, _parse_results, validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "https://localhost/settings",
        "http://10.1.2.3/internal",
        "http://192.168.1.1/router",
        "http://172.16.0.9/panel",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/x",
        "http://0.0.0.0/x",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://printer.local/",
        "https://intranet.internal/doc",
        "",
    ],
)
def test_non_public_targets_are_rejected(url: str):
    with pytest.raises(ReviewError) as caught:
        validate_public_url(url)
    assert caught.value.code == "invalid_params"


def test_public_https_target_passes():
    assert validate_public_url("https://stats.example.org/report") == "https://stats.example.org/report"


def test_fetch_refuses_private_network(tmp_path):
    searcher = WebSearcher()
    with pytest.raises(ReviewError) as caught:
        searcher.fetch("http://127.0.0.1:8080/secret")
    assert caught.value.code == "invalid_params"


def _transport(responses: dict[str, str], *, fail: set[str] | None = None) -> httpx.Client:
    fail = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        target = str(request.url)
        for prefix, body in responses.items():
            if target.startswith(prefix):
                return httpx.Response(200, text=body)
        if target in fail or any(target.startswith(item) for item in fail):
            raise httpx.ConnectError("refused")
        return httpx.Response(404, text="")

    return httpx.Client(transport=httpx.MockTransport(handler))


HTML_RESULTS = (
    '<a class="result__a" href="https://example.org/stats">产业规模公报</a>'
    '<a class="result__snippet" href="#">相关数据片段</a>'
)


def test_search_falls_back_to_second_endpoint():
    primary, fallback = SEARCH_ENDPOINTS[0], SEARCH_ENDPOINTS[1]
    client = _transport({fallback: HTML_RESULTS}, fail={primary})
    hits = WebSearcher(client=client).search("产业规模")
    assert [hit.url for hit in hits] == ["https://example.org/stats"]


def test_search_reports_failure_only_after_all_endpoints():
    client = _transport({}, fail=set(SEARCH_ENDPOINTS))
    with pytest.raises(ReviewError) as caught:
        WebSearcher(client=client).search("产业规模")
    assert caught.value.code == "search_failed"
    assert all(endpoint in caught.value.message for endpoint in SEARCH_ENDPOINTS)


def test_private_results_are_filtered_from_parse():
    html = (
        '<a class="result__a" href="http://192.168.0.10/secret">内网泄露</a>'
        '<a class="result__a" href="https://example.org/public">公开来源</a>'
    )
    hits = _parse_results(html, query="q", limit=5)
    assert [hit.url for hit in hits] == ["https://example.org/public"]


def test_worker_web_fetch_wraps_rejection_as_failed_result(tmp_path):
    import base64

    from tests.helpers import sample_full_thesis_draft
    from thesis_review.worker import Worker

    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    worker.dispatch("open_draft", {"bytes_b64": base64.b64encode(sample_full_thesis_draft()).decode("ascii")})
    result = worker.dispatch("web_fetch", {"url": "http://169.254.169.254/latest/meta-data/"})
    assert result["ok"] is False
    assert result["fabricated"] is False
    assert worker.findings == []
