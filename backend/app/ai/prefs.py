"""Пищевые предпочтения пользователя: аллергии, любимое/нелюбимое, БЖУ, заметка о питании.

Одно глобальное хранилище (авторизации нет): JSON-файл рядом с БД.
- авто-извлечение из сообщений чата моделью задачи `prefs` из настроек (по умолчанию
  Cloudflare; фоновая вспом. задача): дописывает likes/dislikes, а подозрения на аллергию
  кладёт в suggested_allergies —
  сами аллергии экстрактор НИКОГДА не трогает (только ручная правка на /preferences);
- инъекция в промпты генерации (см. prompt.py → as_hint);
- просмотр/правка на экране /preferences (API в routers/chat.py).

Формат файла (snake_case): {allergies, likes, dislikes, suggested_allergies,
macros: {protein, fat, carbs: low|normal|high}, diet_note}. Старый файл (только likes/dislikes)
читается как есть — недостающие поля получают дефолты.
"""

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from ..config import settings
from .gates import cloudflare, gate_for

logger = logging.getLogger("easy_week.prefs")

MAX_ITEMS = 30  # ограничиваем длину списков, чтобы промпт не пух
MAX_ITEM_LEN = 40  # длина одного пункта (длиннее — мусор экстрактора, обрезаем)
MAX_NOTE_LEN = 200  # заметка о питании
MACRO_KEYS = ("protein", "fat", "carbs")
MACRO_LEVELS = ("low", "normal", "high")
LIST_KEYS = ("allergies", "likes", "dislikes", "suggested_allergies")

# Один процесс — один лок: read-modify-write из merge (фон) и PUT не перетирают друг друга.
_lock = threading.Lock()

PREFS_SCHEMA = {
    "type": "object",
    "properties": {
        "dislikes": {"type": "array", "items": {"type": "string"}},
        "likes": {"type": "array", "items": {"type": "string"}},
        "allergies": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["dislikes", "likes", "allergies"],
}

_EXTRACT_SYSTEM = (
    "Ты извлекаешь ТОЛЬКО ЯВНО названные устойчивые пищевые предпочтения из сообщения. "
    "dislikes — что пользователь ЯВНО не любит / просит избегать / не ест "
    "(маркеры: «не люблю», «терпеть не могу», «без …», «убери …», «не ем …»). "
    "allergies — ТОЛЬКО явная аллергия/непереносимость (маркеры: «аллергия на …», "
    "«непереносимость …»); аллергию клади в allergies, НЕ в dislikes. "
    "likes — что пользователь ЯВНО любит / хочет чаще "
    "(маркеры: «люблю», «обожаю», «нравится», «побольше …»). "
    "СТРОГИЕ ПРАВИЛА:\n"
    "1) НИЧЕГО не придумывай и не додумывай — только то, что прямо сказано этими словами.\n"
    "2) НЕ балансируй списки: если сказано только про нелюбимое — likes ПУСТОЙ (и наоборот). "
    "Заполнять списки на каждое сообщение НЕЛЬЗЯ.\n"
    "3) Простое упоминание еды в запросе плана («план с курицей», «5 ужинов», «рыбное на пару») "
    "— это НЕ предпочтение, НЕ добавляй.\n"
    "4) Разовые пожелания к конкретному плану/дню («сегодня хочу», «на этой неделе», «побыстрее») "
    "— НЕ предпочтения.\n"
    "5) ОПЕРАЦИИ НАД ТЕКУЩИМ ПЛАНОМ — это НЕ предпочтения, верни пусто: «убери/замени/добавь "
    "<конкретное блюдо>», «замени X на не-суп», «где суп?!», «верни курицу». Здесь пользователь "
    "правит план, а не рассказывает о вкусах. Особенно когда в контексте сказано, что это правка.\n"
    "6) Контекст (если дан) — ТОЛЬКО чтобы понять, вкус это или разовая правка. Извлекай СТРОГО "
    "из последнего сообщения, из контекста ничего не бери.\n"
    "7) Если ЯВНЫХ предпочтений нет — верни ВСЕ списки пустыми.\n"
    "Названия — короткие, на русском."
)


def _shot(dislikes=(), likes=(), allergies=()) -> dict:
    return {"dislikes": list(dislikes), "likes": list(likes), "allergies": list(allergies)}


# few-shot: маленькая модель иначе «услужливо» заполняет списки на каждое сообщение
_EXTRACT_SHOTS = [
    ("не люблю чечевицу", _shot(dislikes=["чечевица"])),
    ("сделай 5 ужинов побыстрее", _shot()),
    ("обожаю острое, только без грибов", _shot(dislikes=["грибы"], likes=["острое"])),
    # аллергия — отдельно: в профиль попадёт только как подсказка «Добавить в аллергии?»
    ("у меня аллергия на арахис", _shot(allergies=["арахис"])),
    ("хочу план с курицей и рыбой на неделю", _shot()),
    # операции над планом — не предпочтения (частый источник ложных срабатываний)
    ("замени том ям на блюдо не суп", _shot()),
    ("где куриный суп?! я просил один суп", _shot()),
    ("верни куриный, добавь суп в меню", _shot()),
]


def _file() -> Path:
    return Path(settings.db_path).parent / "preferences.json"


def _norm(s: str) -> str:
    return s.strip().lower()


def _has(items: list[str], x: str) -> bool:
    return any(_norm(x) == _norm(y) for y in items)


def _dedup_add(items: list[str], src) -> list[str]:
    """Дописать src в items: без пустых/дублей (без учёта регистра), пункт ≤ MAX_ITEM_LEN."""
    for x in src or []:
        if not isinstance(x, str):
            continue
        x = x.strip()[:MAX_ITEM_LEN].strip()
        if x and not _has(items, x):
            items.append(x)
    return items


def default_macros() -> dict:
    return {k: "normal" for k in MACRO_KEYS}


def normalize(data: dict | None) -> dict:
    """Привести что угодно (в т.ч. старый файл {likes, dislikes}) к полному формату."""
    data = data if isinstance(data, dict) else {}
    out: dict = {k: _dedup_add([], data.get(k))[:MAX_ITEMS] for k in LIST_KEYS}
    raw = data.get("macros") if isinstance(data.get("macros"), dict) else {}
    out["macros"] = {k: raw[k] if raw.get(k) in MACRO_LEVELS else "normal" for k in MACRO_KEYS}
    note = data.get("diet_note")
    out["diet_note"] = note.strip()[:MAX_NOTE_LEN] if isinstance(note, str) else ""
    # предложенное, что уже стало аллергией, больше не предлагаем
    out["suggested_allergies"] = [
        x for x in out["suggested_allergies"] if not _has(out["allergies"], x)
    ]
    return out


def _load_file() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый → пусто
        data = {}
    return normalize(data)


def load() -> dict:
    """Текущие предпочтения в полном формате (обратная совместимость со старым файлом)."""
    return _load_file()


def _save(data: dict) -> None:
    """Атомарная запись: во временный файл рядом + os.replace (полузаписанного JSON не бывает)."""
    try:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except Exception as exc:  # noqa: BLE001 — не должно ронять запрос
        logger.warning("prefs save failed: %s", str(exc)[:120])


def update(patch: dict) -> dict:
    """Ручная правка с экрана /preferences: заменяем ТОЛЬКО переданные поля (не None).

    Частичная замена нужна, чтобы клиент со старым форматом ({likes, dislikes}) не стирал
    аллергии/БЖУ, а фоновый merge между GET и PUT не терял подсказки."""
    with _lock:
        cur = normalize(load())
        for k in LIST_KEYS:
            if patch.get(k) is not None:
                cur[k] = list(patch[k])
        if patch.get("macros") is not None:
            cur["macros"] = {**cur["macros"], **patch["macros"]}
        if patch.get("diet_note") is not None:
            cur["diet_note"] = patch["diet_note"]
        out = normalize(cur)
        _save(out)
        return out


def set_lists(dislikes: list[str], likes: list[str]) -> dict:
    """Совместимость: замена likes/dislikes (аллергии, БЖУ и прочее не трогаем)."""
    return update({"dislikes": dislikes, "likes": likes})


def merge(
    new_dislikes: list[str], new_likes: list[str], new_allergies: list[str] | None = None
) -> dict:
    """Слить извлечённое из чата с накопленным.

    Новый dislike убирает такой же like. Обратное НЕ делаем: лайк из чата никогда не снимает
    dislike/аллергию автоматически (экстрактор ошибается, а цена ошибки — аллерген в рецепте).
    Аллергии экстрактор НЕ меняет: подозрение на аллергию уходит в suggested_allergies,
    а в аллергии его переносит только сам пользователь (чип «Добавить в аллергии?»)."""
    with _lock:
        data = normalize(load())
        allergies = data["allergies"]
        dis, lik, sug = data["dislikes"], data["likes"], data["suggested_allergies"]
        for d in _dedup_add([], new_dislikes):
            if not _has(dis, d):
                dis.append(d)
            lik = [x for x in lik if _norm(x) != _norm(d)]  # был в «люблю» → теперь «не люблю»
        for a in _dedup_add([], new_allergies):
            if not _has(allergies, a) and not _has(sug, a):
                sug.append(a)
            lik = [x for x in lik if _norm(x) != _norm(a)]  # подозрение на аллерген — не «любимое»
        for l in _dedup_add([], new_likes):
            if _has(dis, l) or _has(allergies, l) or _has(sug, l):
                continue  # конфликт с ограничением — ограничение важнее, лайк не пишем
            if not _has(lik, l):
                lik.append(l)
        out = normalize({**data, "dislikes": dis, "likes": lik, "suggested_allergies": sug})
        _save(out)
        return out


# Короткие фразы БЖУ для промпта (normal не пишем — экономим токены).
_MACRO_WORDS = {
    "protein": {"high": "больше белка", "low": "меньше белка"},
    "fat": {"high": "больше жиров", "low": "меньше жиров"},
    "carbs": {"high": "больше углеводов", "low": "меньше углеводов"},
}


def _macros_hint(macros: dict | None) -> str:
    macros = macros or {}
    parts = [_MACRO_WORDS[k][macros[k]] for k in MACRO_KEYS if macros.get(k) in ("high", "low")]
    if not parts:
        return ""  # всё «норма» — ничего не пишем
    return "\nБЖУ: " + ", ".join(parts) + " — подбирай блюда, гарниры и ингредиенты соответственно."


def as_hint(constraints_only: bool = False, macros: bool = True) -> str:
    """Хинт для USER-сообщения промптов генерации (system стабилен). Пусто, если нечего сказать.

    Порядок — по важности: АЛЛЕРГИИ (жёстко, везде, включая следы/соусы) → dislikes (жёстко,
    везде; сюда же неподтверждённые подозрения на аллергию — лучше перестраховаться) →
    likes (мягко, только при ПОДБОРЕ блюд) → БЖУ и заметка о питании (если заданы).
    constraints_only=True — рецепт/деталь уже названного блюда: стилевые likes не подмешиваем
    (иначе «борщ с рыбным соусом»). macros=False — где БЖУ не при чём (план готовки)."""
    data = load()
    out = ""
    allergies = data.get("allergies") or []
    if allergies:
        out += "\nАЛЛЕРГИИ — строго исключить, включая следы/соусы: " + ", ".join(allergies) + "."
    avoid = _dedup_add(list(data.get("dislikes") or []), data.get("suggested_allergies"))
    if avoid:
        out += "\nОграничения пользователя (во ВСЕХ блюдах) — НЕ используй: " + ", ".join(avoid) + "."
    if data.get("likes") and not constraints_only:
        # Любимое — мягко: иначе один лайк («азиатская курица») тянет за собой всё меню.
        out += (
            "\nЛюбимое (учти в 1 блюде плана, не в каждом; явный запрос важнее): "
            + ", ".join(data["likes"]) + "."
        )
    if macros:
        out += _macros_hint(data.get("macros"))
        note = (data.get("diet_note") or "").strip()
        if note:
            out += f"\nО питании пользователя: {note[:MAX_NOTE_LEN]}"
    return out


def avoid_all() -> list[str]:
    """Всё, чего нельзя: аллергии + подозрения + нелюбимое (для зерна разнообразия планера)."""
    data = load()
    out = _dedup_add(list(data.get("allergies") or []), data.get("suggested_allergies"))
    return _dedup_add(out, data.get("dislikes"))


async def extract_and_merge(message: str, context: str = "") -> None:
    """Извлечь предпочтения из сообщения моделью задачи `prefs` (настройки; по умолчанию
    Cloudflare) и слить в профиль.

    context — фон для оценки «вкус или разовая правка плана» (напр. «Это правка плана» +
    пара реплик). Извлечение идёт СТРОГО из message; из контекста ничего не берём.
    Cloudflare — со строгой json_schema и mistral; остальные (OpenRouter/DeepSeek/Gemini) —
    JSON-режим, форма ответа задана few-shot-примерами."""
    msg = (message or "").strip()
    if len(msg) < 3:
        return
    messages: list[dict[str, str]] = [{"role": "system", "content": _EXTRACT_SYSTEM}]
    for shot_in, shot_out in _EXTRACT_SHOTS:
        messages.append({"role": "user", "content": shot_in})
        messages.append({"role": "assistant", "content": json.dumps(shot_out, ensure_ascii=False)})
    ctx = context.strip()
    if ctx:
        final = f"[Контекст — только для оценки, НЕ извлекай из него]\n{ctx}\n\n[Сообщение]: {msg}"
    else:
        final = msg
    messages.append({"role": "user", "content": final})
    gate = gate_for("", "prefs")
    cf_kw = {"schema": PREFS_SCHEMA, "model": settings.cf_model_judge} if gate is cloudflare else {}
    try:
        parsed, _ = await gate.complete_json(
            messages, **cf_kw, max_tokens=200, label="извлечение предпочтений",
        )
        d = parsed.get("dislikes") or []
        l = parsed.get("likes") or []
        a = parsed.get("allergies") or []  # → только suggested_allergies, не в аллергии
        if d or l or a:
            merge(d, l, a)
            logger.info("prefs learned: +dislikes=%s +likes=%s ?allergies=%s", d, l, a)
    except Exception as exc:  # noqa: BLE001 — вспомогательная задача, не критично
        logger.warning("prefs extract skipped: %s", str(exc)[:150])


_tasks: set = set()


def learn_async(message: str, context: str = "") -> None:
    """Запустить извлечение предпочтений фоном — не блокирует основной флоу."""
    if len((message or "").strip()) < 3:
        return
    task = asyncio.create_task(extract_and_merge(message, context))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
