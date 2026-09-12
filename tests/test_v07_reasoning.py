"""V0.7 reasoning effort: configurable, observable, off by default."""
from __future__ import annotations

from pathlib import Path

from tests.helpers import sample_overclaim_draft
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.session_store import model_snapshot
from thesis_review.settings import AppSettings, apply_model
from thesis_review.word.adapter import WordAdapter


def test_reasoning_setting_roundtrip_and_validation(tmp_path: Path):
    settings = AppSettings()
    assert settings.reasoning == "off"
    apply_model(settings, {"reasoning": "high", "model": "test-model"})
    assert settings.reasoning == "high"
    # Unknown levels are ignored rather than guessed at.
    apply_model(settings, {"reasoning": "maximum"})
    assert settings.reasoning == "high"
    apply_model(settings, {"reasoning": ""})
    assert settings.reasoning == "high"


def test_pi_request_carries_reasoning_level(tmp_path: Path, monkeypatch):
    captured: dict = {}

    def fake_run_pi_review(request, *, faux=False, timeout=180):
        captured.update(request)
        out = Path(request["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        redacted = dict(request)
        redacted.pop("api_key", None)
        (out / "pi-request.json").write_text(
            __import__("json").dumps(redacted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        draft_id = request["draft_id"]
        findings = out / f"{draft_id}-findings.json"
        reviewed = out / f"{draft_id}-reviewed.docx"
        source = out / f"{draft_id}-source.docx"
        data = Path(request["draft_path"]).read_bytes()
        source.write_bytes(data)
        reviewed.write_bytes(data)
        findings.write_text("[]", encoding="utf-8")
        return {
            "reviewed_path": str(reviewed),
            "findings_path": str(findings),
            "n_findings": 0,
        }

    import thesis_review.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "run_pi_review", fake_run_pi_review)
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    settings = AppSettings(reasoning="medium", api_key="sk-test")
    service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="overclaim",
        data=sample_overclaim_draft(),
        output_dir=tmp_path / "out",
        use_model=True,
        settings=settings,
        offline_fallback=False,
    )
    assert captured["reasoning"] == "medium"
    # The redacted pi-request.json on disk must also carry it (no key leak).
    import json

    payload = json.loads((tmp_path / "out" / "pi-request.json").read_text(encoding="utf-8"))
    assert payload["reasoning"] == "medium"
    assert "sk-test" not in json.dumps(payload)


def test_model_snapshot_records_reasoning():
    snapshot = model_snapshot(AppSettings(reasoning="low"))
    assert snapshot["reasoning"] == "low"
    # Legacy settings objects without the field still snapshot cleanly.
    legacy = AppSettings()
    object.__delattr__(legacy, "reasoning")
    assert model_snapshot(legacy)["reasoning"] == "off"
