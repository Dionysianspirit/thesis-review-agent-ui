from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from thesis_review.history.match import normalize
from thesis_review.types import IssueRecord, ParagraphView

DEFAULT_THRESHOLD = 0.28
DEFAULT_LIMIT = 6
# Hybrid recall anchors: numbers and latin terms survive paraphrase, so they
# are strong signals; pure-Chinese rewriting still relies on bigram cosine.
ANCHOR_TOKEN_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:%|％)?|[A-Za-z][A-Za-z0-9_-]{1,}")
ANCHOR_BLEND = 0.4


@dataclass(frozen=True)
class SemanticHit:
    issue_id: str
    new_anchor: str
    new_quote: str
    paragraph_index: int
    score: float
    original_text: str
    original_span: str
    problem: str
    category: str
    method: str = "ngram"


def semantic_recall(
    issues: list[IssueRecord],
    paragraphs: list[ParagraphView],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
) -> list[SemanticHit]:
    """Rank current-draft paragraphs against student history. Never marks recidivism."""
    # Paragraph grams and norms are issue-independent; compute once per call
    # instead of once per (issue, query, paragraph) triple.
    para_data = []
    for paragraph in paragraphs:
        if not paragraph.text.strip():
            continue
        grams = _grams(paragraph.text)
        para_data.append((grams, _vector_norm(grams), paragraph))
    ranked: list[SemanticHit] = []
    for issue in issues:
        queries = [part for part in (issue.original_span, issue.original_text, issue.problem, _issue_query(issue)) if part]
        if not queries:
            continue
        query_data = []
        for query in queries:
            grams = _grams(query)
            query_data.append((grams, _vector_norm(grams)))
        best: SemanticHit | None = None
        for grams, para_norm, paragraph in para_data:
            score = max(
                _cosine_precomputed(query_grams, query_norm, grams, para_norm)
                for query_grams, query_norm in query_data
            )
            if score < threshold:
                continue
            hit = SemanticHit(
                issue_id=issue.id,
                new_anchor=paragraph.anchor,
                new_quote=_clip(paragraph.text),
                paragraph_index=paragraph.ordinal,
                score=score,
                original_text=issue.original_text,
                original_span=issue.original_span,
                problem=issue.problem or issue.original_text,
                category=issue.category,
            )
            if best is None or hit.score > best.score:
                best = hit
        if best is not None:
            ranked.append(best)
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[: max(1, limit)] if ranked else []


def _issue_query(issue: IssueRecord) -> str:
    parts = [
        issue.problem,
        issue.original_text,
        issue.original_span,
        issue.teacher_intent,
        issue.suggested_fix,
    ]
    return "。".join(part for part in parts if part)


def anchor_tokens(text: str) -> Counter[str]:
    """Numbers and latin terms: paraphrase-resistant anchor vocabulary."""
    tokens = ANCHOR_TOKEN_RE.findall(normalize(text))
    return Counter(token.lower() for token in tokens)


def _anchor_overlap(query: Counter[str], paragraph: Counter[str]) -> float:
    if not query:
        return 0.0
    total = sum(query.values())
    hit = sum(weight for token, weight in query.items() if paragraph.get(token, 0) > 0)
    return hit / total


def hybrid_recall(
    issues: list[IssueRecord],
    paragraphs: list[ParagraphView],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
) -> list[SemanticHit]:
    """Bigram cosine blended with number/latin anchor overlap.

    A long paragraph dilutes character-bigram cosine below threshold even when
    the exact figure from a confirmed history issue (e.g. "81%") is present.
    Anchors fix exactly that: score = max(cosine, 0.6*cosine + 0.4*overlap),
    so recall still needs partial textual agreement plus anchor evidence —
    anchor hits alone never clear the threshold. Still recall-only: hits are
    context-check candidates, never verdicts.
    """
    para_data = []
    for paragraph in paragraphs:
        if not paragraph.text.strip():
            continue
        grams = _grams(paragraph.text)
        para_data.append((grams, _vector_norm(grams), anchor_tokens(paragraph.text), paragraph))
    ranked: list[SemanticHit] = []
    for issue in issues:
        queries = [part for part in (issue.original_span, issue.original_text, issue.problem, _issue_query(issue)) if part]
        if not queries:
            continue
        query_data = []
        for query in queries:
            grams = _grams(query)
            query_data.append((grams, _vector_norm(grams)))
        query_anchors: Counter[str] = Counter()
        for query in queries:
            query_anchors.update(anchor_tokens(query))
        best: SemanticHit | None = None
        for grams, para_norm, anchors, paragraph in para_data:
            cosine = max(
                _cosine_precomputed(query_grams, query_norm, grams, para_norm)
                for query_grams, query_norm in query_data
            )
            score = cosine
            if query_anchors:
                score = max(cosine, cosine * (1 - ANCHOR_BLEND) + _anchor_overlap(query_anchors, anchors) * ANCHOR_BLEND)
            if score < threshold:
                continue
            hit = SemanticHit(
                issue_id=issue.id,
                new_anchor=paragraph.anchor,
                new_quote=_clip(paragraph.text),
                paragraph_index=paragraph.ordinal,
                score=score,
                original_text=issue.original_text,
                original_span=issue.original_span,
                problem=issue.problem or issue.original_text,
                category=issue.category,
                method="hybrid" if query_anchors else "ngram",
            )
            if best is None or hit.score > best.score:
                best = hit
        if best is not None:
            ranked.append(best)
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[: max(1, limit)] if ranked else []


def _grams(text: str) -> Counter[str]:
    compact = normalize(text)
    if len(compact) < 2:
        return Counter({compact: 1} if compact else {})
    return Counter(compact[index : index + 2] for index in range(len(compact) - 1))


def _vector_norm(counter: Counter[str]) -> float:
    return sum(value * value for value in counter.values()) ** 0.5


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    return _cosine_precomputed(left, _vector_norm(left), right, _vector_norm(right))


def _cosine_precomputed(
    left: Counter[str],
    left_norm: float,
    right: Counter[str],
    right_norm: float,
) -> float:
    # Same key iteration order and same arithmetic as the naive _cosine so the
    # precomputed-norm callers stay bit-identical, not just mathematically equal.
    if not left or not right or not left_norm or not right_norm:
        return 0.0
    keys = set(left) | set(right)
    dot = sum(left[key] * right[key] for key in keys)
    return dot / (left_norm * right_norm)


def _clip(text: str, limit: int = 160) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit]
