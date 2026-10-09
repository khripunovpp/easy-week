"""Пищевые предпочтения пользователя: аллергии, любимое/нелюбимое, БЖУ, заметка о питании.

Одно глобальное хранилище (авторизации нет): JSON-файл рядом с БД.
- авто-извлечение из сообщений чата моделью задачи `prefs` из настроек (по умолчанию
  Cloudflare; фоновая вспом. задача). Три уровня уверенности: СТОПРОЦЕНТНОЕ и постоянное
  («не ем», «терпеть не могу», «никогда не предлагай», общее «люблю X») → сразу в
  likes/dislikes; ПОХОЖЕ на вкус, но не наверняка → в suggested_dislikes/suggested_likes
  (подсказка «Добавить?» на /preferences, решает пользователь); РАЗОВОЕ («на этой неделе без
  рыбы», «убери суп») → никуда (внутри чата его учитывает память беседы). Страховки без модели:
  зовём экстрактор только при словах про вкус, разовые маркеры без слов про постоянство
  понижают уверенность до подсказки, продукт должен встречаться в тексте. Подозрения на аллергию
  кладёт в suggested_allergies —
  сами аллергии экстрактор НИКОГДА не трогает (только ручная правка на /preferences);
- инъекция в промпты генерации (см. prompt.py → as_hint);
- просмотр/правка на экране /preferences (API в routers/chat.py).

Формат файла (snake_case): {allergies, likes, dislikes, suggested_allergies, suggested_dislikes,
suggested_likes, macros: {protein, fat, carbs: low|normal|high}, diet_note}. Старый файл (только likes/dislikes)
читается как есть — недостающие поля получают дефолты.
"""

import asyncio
import json
import logging
import os
import re
import threading
from pathlib import Path

from ..config import settings
from .gates import cf_main, cloudflare, gate_for

logger = logging.getLogger("easy_week.prefs")

MAX_ITEMS = 30  # ограничиваем длину списков, чтобы промпт не пух
MAX_ITEM_LEN = 40  # длина одного пункта (длиннее — мусор экстрактора, обрезаем)
MAX_NOTE_LEN = 200  # заметка о питании
MACRO_KEYS = ("protein", "fat", "carbs")
MACRO_LEVELS = ("low", "normal", "high")
LIST_KEYS = (
    "allergies", "likes", "dislikes", "suggested_allergies", "suggested_dislikes", "suggested_likes",
)

# Один процесс — один лок: read-modify-write из merge (фон) и PUT не перетирают друг друга.
_lock = threading.Lock()

_ARR = {"type": "array", "items": {"type": "string"}}
PREFS_SCHEMA = {
    "type": "object",
    "properties": {
        "dislikes": _ARR, "likes": _ARR, "maybe_dislikes": _ARR, "maybe_likes": _ARR,
        "allergies": _ARR,
    },
    "required": ["dislikes", "likes", "maybe_dislikes", "maybe_likes", "allergies"],
}

_EXTRACT_SYSTEM = (
    "Ты ведёшь профиль вкусов пользователя. dislikes — это список «БОЛЬШЕ НИКОГДА НЕ "
    "ПРЕДЛАГАТЬ», likes — «любит всегда». Ошибка здесь дорогая: блюдо навсегда пропадёт из "
    "меню. Поэтому раскладывай ТОЛЬКО то, что прямо сказано в последнем сообщении, по трём "
    "уровням уверенности:\n"
    "1) dislikes / likes — ТОЛЬКО стопроцентное и ПОСТОЯННОЕ: «не ем», «мы не едим», «терпеть "
    "не могу», «ненавижу», «никогда не предлагай», «вообще не люблю», общее «не люблю X» / "
    "«люблю X» / «обожаю X» без привязки к неделе или плану.\n"
    "2) maybe_dislikes / maybe_likes — похоже на вкус, но НЕ наверняка: «не очень люблю», "
    "«что-то не нравится», «кажется, не люблю», «понравилось это блюдо», «было вкусно», вкус, "
    "упомянутый вместе со словами «на этой неделе» / «в этот раз».\n"
    "3) Никуда (пустые списки) — РАЗОВОЕ и ОПЕРАЦИИ С ПЛАНОМ: «на этой неделе без рыбы», "
    "«в этот раз не хочу супов», «давай без свинины», «убери / замени / добавь X», «сделай "
    "побыстрее», «надоела курица», «хочу план с курицей», просто упоминание еды в запросе. "
    "Слова «без …», «убери …», «не хочу» САМИ ПО СЕБЕ — это разовое, не вкус.\n"
    "allergies — ТОЛЬКО явная аллергия/непереносимость («аллергия на …», «непереносимость …»); "
    "аллергию клади в allergies, НЕ в dislikes.\n"
    "Правила: ничего не додумывай; не балансируй списки (заполнять их на каждое сообщение "
    "НЕЛЬЗЯ); контекст (если дан) — только чтобы понять, вкус это или разовая правка, извлекай "
    "СТРОГО из последнего сообщения; сомневаешься между 1 и 2 — выбирай 2, между 2 и 3 — выбирай 3. "
    "Названия — короткие, на русском, как в сообщении."
)


def _shot(dislikes=(), likes=(), allergies=(), maybe_dislikes=(), maybe_likes=()) -> dict:
    return {
        "dislikes": list(dislikes), "likes": list(likes),
        "maybe_dislikes": list(maybe_dislikes), "maybe_likes": list(maybe_likes),
        "allergies": list(allergies),
    }


# few-shot: маленькая модель иначе «услужливо» заполняет списки на каждое сообщение
_EXTRACT_SHOTS = [
    ("не люблю чечевицу", _shot(dislikes=["чечевица"])),
    ("сделай 5 ужинов побыстрее", _shot()),
    # разовое: неделя/план — не вкус (главный источник ложных «не люблю»)
    ("в эту неделю давай без рыбы", _shot()),
    ("на этой неделе не хочу супов, остальное как обычно", _shot()),
    ("обожаю острое, только без грибов", _shot(likes=["острое"])),
    ("мы вообще не едим свинину, никогда её не предлагай", _shot(dislikes=["свинина"])),
    ("печень терпеть не могу", _shot(dislikes=["печень"])),
    # похоже на вкус, но не наверняка — только подсказка пользователю
    ("баклажаны что-то не очень люблю", _shot(maybe_dislikes=["баклажаны"])),
    ("солянка прошлая очень понравилась", _shot(maybe_likes=["солянка"])),
    # аллергия — отдельно: в профиль попадёт только как подсказка «Добавить в аллергии?»
    ("у меня аллергия на арахис", _shot(allergies=["арахис"])),
    ("хочу план с курицей и рыбой на неделю", _shot()),
    # операции над планом — не предпочтения
    ("замени том ям на блюдо не суп", _shot()),
    ("убери грибы из рецепта", _shot()),
    ("где куриный суп?! я просил один суп", _shot()),
    ("надоела курица, давай в этот раз говядину", _shot()),
    ("что-то сейчас не хочется рыбы", _shot()),
    ("я люблю солянку, давай добавим к ней пару блюд", _shot(likes=["солянка"])),
]

# --- Страховки без модели (детерминированно, до и после вызова) ---
# Слова про вкус: без них экстрактор не зовём вовсе (запросы плана и правки — мимо).
_TASTE_MARKERS = (
    "любл", "люби", "обожа", "нравит", "нравл", "понрав", "ненавиж", "терпеть не", "не ем",
    "не едим", "не ест ", "не перенош", "не выношу", "аллерг", "непереносим", "никогда",
    "противн", "мерзк", "отвратит", "не перевар", "вкусн", "не очень",
)
# Постоянство: с ними разовые маркеры не понижают уверенность.
_PERMANENT_MARKERS = (
    "вообще", "никогда", "всегда", "терпеть не", "ненавиж", "не ем", "не едим", "аллерг",
    "непереносим", "в принципе", "совсем не", "с детства", "по жизни",
)
# Разовое: неделя/план/сейчас — без слов про постоянство вкус максимум «похоже».
_TEMPORARY_MARKERS = (
    "на этой неделе", "на эту неделю", "в эту неделю", "этой недели", "на следующей неделе",
    "на следующую неделю", "в этот раз", "на этот раз", "в этом плане", "в этот план", "сегодня",
    "сейчас", "пока что", "на неделю",
)


def _norm_text(s: str) -> str:
    return " ".join((s or "").lower().replace("ё", "е").split())


def has_taste_marker(message: str) -> bool:
    t = _norm_text(message)
    return any(m in t for m in _TASTE_MARKERS)


def _is_temporary(message: str) -> bool:
    t = _norm_text(message)
    return any(m in t for m in _TEMPORARY_MARKERS) and not any(m in t for m in _PERMANENT_MARKERS)


def _is_review(message: str) -> bool:
    """«Понравилось / было вкусно» без «люблю / обожаю» — отзыв о конкретном блюде, не
    устойчивый вкус: максимум подсказка."""
    t = _norm_text(message)
    return any(m in t for m in ("понрав", "вкусн")) and not any(m in t for m in ("любл", "обожа"))


def has_permanent_marker(message: str) -> bool:
    """«вообще», «никогда», «терпеть не могу»… — устойчивый вкус, даже в пожелании к кнопке."""
    t = _norm_text(message)
    return any(m in t for m in _PERMANENT_MARKERS)


def _short_forms(w: str) -> set[str]:
    """Падежи короткого слова: «щи» → щи, щей, щам, щами, щах."""
    return {w} | {w[:-1] + end for end in ("ей", "ам", "ами", "ах")}


def grounded(items, message: str) -> list[str]:
    """Только продукты, которые реально есть в тексте (основа слова ≥3 букв) — модель не
    должна додумывать «картошка с бабами» из «картошка ой». Продукт из одних коротких слов
    («щи») — целым словом или падежом: раньше он отбрасывался всегда (👎 «не люблю щи»)."""
    t = _norm_text(message)
    tokens = set(re.findall(r"[а-яa-z]+", t))
    out: list[str] = []
    for x in items or []:
        if not isinstance(x, str):
            continue
        all_words = re.findall(r"[а-яa-z]+", _norm_text(x))
        words = [w for w in all_words if len(w) >= 3]
        if words:
            ok = any(w[: max(3, len(w) - 2)] in t for w in words)
        else:
            ok = any(_short_forms(w) & tokens for w in all_words if len(w) == 2)
        if ok:
            out.append(x)
    return out


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
    # подсказки вкусов — только то, что ещё не решено пользователем
    decided = out["allergies"] + out["dislikes"] + out["likes"]
    out["suggested_dislikes"] = [x for x in out["suggested_dislikes"] if not _has(decided, x)]
    out["suggested_likes"] = [
        x for x in out["suggested_likes"]
        if not _has(decided, x) and not _has(out["suggested_dislikes"], x)
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
    new_dislikes: list[str], new_likes: list[str], new_allergies: list[str] | None = None,
    maybe_dislikes: list[str] | None = None, maybe_likes: list[str] | None = None,
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
        # «Похоже на вкус» — только подсказки «Добавить?»: в dislikes/likes их переносит пользователь.
        sd = _dedup_add(list(data["suggested_dislikes"]), maybe_dislikes)
        sl = _dedup_add(list(data["suggested_likes"]), maybe_likes)
        out = normalize({
            **data, "dislikes": dis, "likes": lik, "suggested_allergies": sug,
            "suggested_dislikes": sd, "suggested_likes": sl,
        })
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
    Cloudflare) и слить в профиль: стопроцентное — в likes/dislikes, «похоже» — в подсказки,
    разовое — никуда.

    context — фон для оценки «вкус или разовая правка плана» (напр. «Это правка плана» +
    пара реплик). Извлечение идёт СТРОГО из message; из контекста ничего не берём.
    Cloudflare — со строгой json_schema и mistral; остальные (OpenRouter/DeepSeek/Gemini) —
    JSON-режим, форма ответа задана few-shot-примерами."""
    msg = (message or "").strip()
    if len(msg) < 3:
        return
    if not has_taste_marker(msg):
        return  # запрос плана / правка без слов про вкус — модель не зовём (и не ошибётся)
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
    cf_kw = {"schema": PREFS_SCHEMA, "model": cf_main(gate)} if (gate is cloudflare or getattr(gate, "key", "") == "cloudflare") else {}
    try:
        parsed, _ = await gate.complete_json(
            messages, **cf_kw, max_tokens=250, temperature=0.1, label="извлечение предпочтений",
        )
        got = {k: grounded(parsed.get(k), msg) for k in PREFS_SCHEMA["properties"]}
        d, l, a = got["dislikes"], got["likes"], got["allergies"]
        md, ml = got["maybe_dislikes"], got["maybe_likes"]
        if _is_temporary(msg):
            # «на этой неделе…» без слов про постоянство — максимум подсказка, не «никогда».
            md, ml, d, l = md + d, ml + l, [], []
        if _is_review(msg):
            ml, l = ml + l, []
        # уже уверенное не дублируем подсказкой
        md = [x for x in md if not _has(d, x)]
        ml = [x for x in ml if not _has(l, x)]
        if d or l or a or md or ml:
            merge(d, l, a, md, ml)
            logger.info(
                "prefs learned: +dislikes=%s +likes=%s ?dislikes=%s ?likes=%s ?allergies=%s",
                d, l, md, ml, a,
            )
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
