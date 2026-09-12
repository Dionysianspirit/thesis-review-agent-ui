from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AppSettings:
    teacher_id: str = "teacher-a"
    teacher_name: str = "老师甲"
    student_id: str = "zhou"
    student_name: str = "小周"
    major: str = "人工智能"
    provider: str = "openai-compatible"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    # Reasoning effort for the first-pass agent. "off" keeps V0.6 behavior;
    # low/medium/high pass through to the agent runtime. Left configurable
    # because whether higher effort helps data/method/experiment review can
    # only be decided from real-model eval numbers.
    reasoning: str = "off"
    output_dir: str = ""
    last_reviewed_path: str = ""
    last_output_dir: str = ""
    last_paper_path: str = ""
    last_session_id: str = ""


REASONING_LEVELS = frozenset({"off", "low", "medium", "high"})


def settings_path(home: Path) -> Path:
    return home / "settings.json"


def load_settings(home: Path) -> AppSettings:
    path = settings_path(home)
    if not path.is_file():
        return AppSettings()
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in AppSettings.__dataclass_fields__.values()}
    return AppSettings(**{key: value for key, value in raw.items() if key in allowed})


def save_settings(home: Path, settings: AppSettings) -> None:
    home.mkdir(parents=True, exist_ok=True)
    settings_path(home).write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def apply_model(settings: AppSettings, payload: dict) -> AppSettings:
    settings.provider = str(payload.get("provider") or settings.provider or "openai-compatible")
    settings.model = str(payload.get("model") or settings.model or "")
    settings.base_url = str(payload.get("base_url") or "")
    reasoning = str(payload.get("reasoning") or "").strip().lower()
    if reasoning in REASONING_LEVELS:
        settings.reasoning = reasoning
    key = str(payload.get("api_key") or "").strip()
    if key:
        settings.api_key = key
    return settings


def public_settings(settings: AppSettings) -> dict:
    payload = asdict(settings)
    payload["api_key_set"] = bool(settings.api_key.strip())
    payload["api_key"] = ""
    return payload
