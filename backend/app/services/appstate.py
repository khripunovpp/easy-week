"""Глобальное состояние приложения (без авторизации, одно на всех): выбранный «текущий» план
для покупок и готовки. JSON-файл рядом с БД, как у предпочтений (prefs)."""

import json
from pathlib import Path

from ..config import settings


def _file() -> Path:
    return Path(settings.db_path).parent / "app_state.json"


def get_current_plan() -> str | None:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data.get("current_plan_id") or None
    except Exception:  # noqa: BLE001 — нет файла/битый → не выбран
        return None


def set_current_plan(plan_id: str | None) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"current_plan_id": plan_id}, ensure_ascii=False), encoding="utf-8")
