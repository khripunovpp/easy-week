"""Пищевые предпочтения пользователя (что любит / не любит).

Одно глобальное хранилище (авторизации нет): JSON-файл рядом с БД.
- авто-извлечение из сообщений чата бесплатной моделью Cloudflare (фоновая вспом. задача);
- инъекция в промпты генерации (см. prompt.py → as_hint);
- просмотр/правка из профиля (API в routers/chat.py).
"""

import asyncio
import json
import logging
from pathlib import Path

from ..config import settings
from .gates import cloudflare

logger = logging.getLogger("easy_week.prefs")

_MAX = 30  # ограничиваем длину списков, чтобы промпт не пух

PREFS_SCHEMA = {
    "type": "object",
    "properties": {
        "dislikes": {"type": "array", "items": {"type": "string"}},
        "likes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["dislikes", "likes"],
}

_EXTRACT_SYSTEM = (
    "Ты извлекаешь ТОЛЬКО ЯВНО названные устойчивые пищевые предпочтения из сообщения. "
    "dislikes — что пользователь ЯВНО не любит / просит избегать / не ест / аллергия "
    "(маркеры: «не люблю», «терпеть не могу», «без …», «убери …», «аллергия на …», «не ем …»). "
    "likes — что пользователь ЯВНО любит / хочет чаще "
    "(маркеры: «люблю», «обожаю», «нравится», «побольше …»). "
    "СТРОГИЕ ПРАВИЛА:\n"
    "1) НИЧЕГО не придумывай и не додумывай — только то, что прямо сказано этими словами.\n"
    "2) НЕ балансируй списки: если сказано только про нелюбимое — likes ПУСТОЙ (и наоборот). "
    "Заполнять оба списка на каждое сообщение НЕЛЬЗЯ.\n"
    "3) Простое упоминание еды в запросе плана («план с курицей», «5 ужинов», «рыбное на пару») "
    "— это НЕ предпочтение, НЕ добавляй.\n"
    "4) Разовые пожелания к конкретному плану/дню («сегодня хочу», «на этой неделе», «побыстрее») "
    "— НЕ предпочтения.\n"
    "5) Если ЯВНЫХ предпочтений нет — верни ОБА списка пустыми.\n"
    "Названия — короткие, на русском."
)

# few-shot: маленькая модель иначе «услужливо» заполняет оба списка на каждое сообщение
_EXTRACT_SHOTS = [
    ("не люблю чечевицу", {"dislikes": ["чечевица"], "likes": []}),
    ("сделай 5 ужинов побыстрее", {"dislikes": [], "likes": []}),
    ("обожаю острое, только без грибов", {"dislikes": ["грибы"], "likes": ["острое"]}),
    ("хочу план с курицей и рыбой на неделю", {"dislikes": [], "likes": []}),
]


def _file() -> Path:
    return Path(settings.db_path).parent / "preferences.json"


def load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый → пусто
        data = {}
    return {"dislikes": data.get("dislikes") or [], "likes": data.get("likes") or []}


def _save(data: dict) -> None:
    try:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — не должно ронять запрос
        logger.warning("prefs save failed: %s", str(exc)[:120])


def _norm(s: str) -> str:
    return s.strip().lower()


def _dedup_add(items: list[str], src: list[str]) -> list[str]:
    for x in src or []:
        x = x.strip()
        if x and not any(_norm(x) == _norm(y) for y in items):
            items.append(x)
    return items


def set_lists(dislikes: list[str], likes: list[str]) -> dict:
    """Полная замена (правка из профиля): дедуп + обрезка."""
    out = {
        "dislikes": _dedup_add([], dislikes)[:_MAX],
        "likes": _dedup_add([], likes)[:_MAX],
    }
    _save(out)
    return out


def merge(new_dislikes: list[str], new_likes: list[str]) -> dict:
    """Слить извлечённое из чата с накопленным. Конфликт — новее выигрывает."""
    data = load()
    dis, lik = data["dislikes"], data["likes"]
    for d in new_dislikes or []:
        d = d.strip()
        if not d:
            continue
        if not any(_norm(d) == _norm(x) for x in dis):
            dis.append(d)
        lik = [x for x in lik if _norm(x) != _norm(d)]  # был в «люблю» → теперь «не люблю»
    for l in new_likes or []:
        l = l.strip()
        if not l:
            continue
        if not any(_norm(l) == _norm(x) for x in lik):
            lik.append(l)
        dis = [x for x in dis if _norm(x) != _norm(l)]
    out = {"dislikes": dis[:_MAX], "likes": lik[:_MAX]}
    _save(out)
    return out


def as_hint() -> str:
    """Хинт для промптов генерации. Пусто, если предпочтений нет."""
    data = load()
    parts = []
    if data["dislikes"]:
        parts.append("НЕ используй и избегай: " + ", ".join(data["dislikes"]))
    if data["likes"]:
        parts.append("по возможности предпочитай: " + ", ".join(data["likes"]))
    if not parts:
        return ""
    return (
        "\nПредпочтения пользователя (учитывай во ВСЕХ блюдах и ингредиентах): "
        + "; ".join(parts)
        + "."
    )


async def extract_and_merge(message: str) -> None:
    """Извлечь предпочтения из сообщения (Cloudflare, бесплатно) и слить в профиль."""
    msg = (message or "").strip()
    if len(msg) < 3:
        return
    messages: list[dict[str, str]] = [{"role": "system", "content": _EXTRACT_SYSTEM}]
    for shot_in, shot_out in _EXTRACT_SHOTS:
        messages.append({"role": "user", "content": shot_in})
        messages.append({"role": "assistant", "content": json.dumps(shot_out, ensure_ascii=False)})
    messages.append({"role": "user", "content": msg})
    try:
        parsed, _ = await cloudflare.complete_json(
            messages,
            schema=PREFS_SCHEMA,
            model=settings.cf_model_judge,
            max_tokens=200,
            label="извлечение предпочтений",
        )
        d = parsed.get("dislikes") or []
        l = parsed.get("likes") or []
        if d or l:
            merge(d, l)
            logger.info("prefs learned: +dislikes=%s +likes=%s", d, l)
    except Exception as exc:  # noqa: BLE001 — вспомогательная задача, не критично
        logger.warning("prefs extract skipped: %s", str(exc)[:150])


_tasks: set = set()


def learn_async(message: str) -> None:
    """Запустить извлечение предпочтений фоном — не блокирует основной флоу."""
    if len((message or "").strip()) < 3:
        return
    task = asyncio.create_task(extract_and_merge(message))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
