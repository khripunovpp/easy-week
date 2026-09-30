import asyncio
import difflib
import logging
import random
import re
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

from ..config import settings
from .base import AIError
from .gates import cf_main, cf_menu, cloudflare, gate_for
from .limits import enforce_daily
from .observe import set_ai_context
from . import prefs as _prefs
from .prompt import (
    COOKPLAN_SCHEMA,
    DISCUSS_SCHEMA,
    DISCUSS_TOOLS,
    DISH_DETAIL_SCHEMA,
    DISH_SCHEMA,
    EDIT_ACTION_SCHEMA,
    NAMES_SCHEMA,
    PLAN_TOOLS,
    SHOP_SCHEMA,
    SINGLE_DISH_SCHEMA,
    VALIDATE_SCHEMA,
    build_cook_plan_messages,
    build_dish_detail_messages,
    build_discuss_messages,
    build_dish_messages,
    build_ds_plan_messages,
    build_edit_action_messages,
    build_edit_messages,
    build_names_messages,
    build_shop_normalize_messages,
    build_single_dish_messages,
    build_validate_messages,
)
from .stream_parse import PlanStreamParser
from ..services.variants import with_detail

logger = logging.getLogger("easy_week.planner")


def _is_cf(gate) -> bool:
    """Cloudflare-гейт, в т.ч. копия с моделью из настроек (сравнение по ключу, не по объекту)."""
    return gate is cloudflare or getattr(gate, "key", "") == "cloudflare"

_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# count из селектора — дефолт; явное число в сообщении важнее (см. промпт).
# Поэтому не режем до count, а лишь ограничиваем разумным максимумом.
_MAX_DISHES = 12

# Температура генерации НОВОГО плана (DeepSeek/Gemini): выше дефолта — ради разнообразия.
# Claude температуру не принимает (гейт её не шлёт), Cloudflare — тоже без неё.
# Правки (tools/actions) остаются на 0.3 (см. гейты), одно блюдо — дефолт гейта.
_PLAN_TEMPERATURE = 1.0


def _dishes_word(n: int) -> str:
    """«1 блюдо» / «3 блюда» / «5 блюд» — для лейблов логов."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} блюдо"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} блюда"
    return f"{n} блюд"


_SEASONS = {12: "зима", 1: "зима", 2: "зима", 3: "весна", 4: "весна", 5: "весна",
            6: "лето", 7: "лето", 8: "лето", 9: "осень", 10: "осень", 11: "осень"}


def _date_hint(today: date | None = None) -> str:
    """Неделя плана + сезон — модель подбирает сезонные продукты (раньше даты не знала)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    return f"Неделя плана: {_week_label(today)}, {_SEASONS[monday.month]} — уместны сезонные продукты."


# --- Серверное «зерно разнообразия» -------------------------------------------------------
# Причина однообразия — детерминированный вход (одинаковый промпт → одинаковое меню), а не
# только температура. Поэтому для каждого НОВОГО плана случайно выбираем акцент кухни,
# 1–2 способа готовки и 2 основных продукта (реже встречавшиеся в недавней истории — с
# бо́льшим весом; нелюбимое исключаем). Подсказка мягкая: явный запрос пользователя важнее.

_CUISINES = (
    "русская домашняя", "грузинская", "итальянская", "французская домашняя",
    "средиземноморская", "балканская", "узбекская", "мексиканская", "индийская",
    "корейская", "японская", "китайская", "тайская", "ближневосточная", "скандинавская",
)
_METHODS = (
    "тушение", "запекание в духовке", "томление в горшочке", "рагу", "суп или похлёбка",
    "фарширование", "запеканка", "котлеты или тефтели", "обжарка с соусом",
    "плов или крупа с мясом",
)
# Основной продукт → основы слов для поиска в названиях (история, нелюбимое).
_PROTEINS: dict[str, tuple[str, ...]] = {
    "курица или индейка": ("кур", "цыпл", "индейк", "птиц", "утк"),
    "говядина": ("говя", "телят", "бефстр", "ростбиф"),
    "свинина": ("свин", "буженин", "корейк"),
    "баранина": ("баран", "ягн"),
    "рыба": ("рыб", "лосос", "семг", "треск", "минта", "судак", "горбуш", "скумбр", "тунц",
             "хек", "форел", "окун"),
    "морепродукты": ("кревет", "кальмар", "мид", "морепр"),
    "бобовые": ("фасол", "нут", "чечевиц", "горох", "боб", "маш"),
    "яйца или творог": ("яйц", "омлет", "творог", "сырник", "фриттат"),
    "грибы и овощи": ("гриб", "овощ", "баклажан", "кабач", "тыкв", "капуст", "шампин"),
}


def _norm_text(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def _protein_counts(names: list[str]) -> dict[str, int]:
    """Сколько раз каждый основной продукт встречается в названиях (по основам слов)."""
    counts = {p: 0 for p in _PROTEINS}
    for n in names:
        t = _norm_text(n)
        for p, stems in _PROTEINS.items():
            if any(st in t for st in stems):
                counts[p] += 1
    return counts


def _disliked_proteins(dislikes: list[str]) -> set[str]:
    """Основные продукты, задетые нелюбимым (напр. «рыба», «свинина») — их не предлагаем."""
    out: set[str] = set()
    for d in dislikes:
        t = _norm_text(d)
        for p, stems in _PROTEINS.items():
            if any(st in t for st in stems):
                out.add(p)
    return out


def _variety_hint(
    history: list[str], dislikes: list[str] | None = None, rng: random.Random | None = None
) -> tuple[str, dict]:
    """Зерно разнообразия для нового плана → (текст для user-сообщения, dict для лога).

    history — «недавно ели или отвергли» (частые там продукты получают меньший вес),
    dislikes — нелюбимое пользователя (такие продукты не предлагаем вовсе)."""
    rng = rng or random.Random()
    if dislikes is None:
        dislikes = _prefs.avoid_all()  # аллергии + подозрения + нелюбимое
    counts = _protein_counts(history)
    banned = _disliked_proteins(dislikes)
    pool = [p for p in _PROTEINS if p not in banned]
    proteins: list[str] = []
    while pool and len(proteins) < 2:  # взвешенная выборка без повторов: вес 1/(1+частота)
        weights = [1.0 / (1 + counts[p]) for p in pool]
        pick = rng.choices(pool, weights=weights, k=1)[0]
        proteins.append(pick)
        pool.remove(pick)
    cuisine = rng.choice(_CUISINES)
    methods = rng.sample(_METHODS, k=rng.choice((1, 2)))
    seed = {"cuisine": cuisine, "methods": methods, "proteins": proteins}
    text = (
        "Для разнообразия (мягко, явный запрос пользователя важнее): кухня-акцент для 1 блюда — "
        f"{cuisine}; способы: {', '.join(methods)}"
    )
    if proteins:
        text += f"; чаще основа: {', '.join(proteins)}"
    return text + ".", seed


# Лёгкая подсказка для добавления нескольких блюд в готовый план (add_dishes, count>1).
_NEIGHBOR_HINT = "Не повторяй основной продукт и способ готовки соседних блюд плана."


def _week_label(today: date | None = None) -> str:
    # План всегда на СЛЕДУЮЩУЮ неделю: начинаешь чат на этой неделе — готовишь
    # в ближайшие выходные на неделю с понедельника (Пн–Вс следующей недели).
    today = today or date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {_MONTHS_GEN[sunday.month - 1]}"
    return (
        f"{monday.day} {_MONTHS_GEN[monday.month - 1]} – "
        f"{sunday.day} {_MONTHS_GEN[sunday.month - 1]}"
    )


def _clean_title(title: str) -> str:
    title = re.sub(r"\s*\([^)]*\)", "", title).strip(" .,-")
    if len(title) > 34:
        title = title[:34].rsplit(" ", 1)[0] + "…"
    return title or "План на неделю"


def _slug(text: str, i: int) -> str:
    base = re.sub(r"[^a-zа-я0-9]+", "-", text.lower()).strip("-")
    return f"dish-{i}-{base}"[:48] or f"dish-{i}"


def _clean_name(name: str) -> str:
    # Убираем описания в скобках и лишние хвосты, которые иногда добавляет модель.
    name = re.sub(r"\s*\([^)]*\)", "", name)
    return name.strip(" .,-") or "Блюдо"


async def _gen_dish(i: int, name: str, emoji: str, user_message: str) -> dict[str, Any]:
    """Спеки одного блюда — часть пайплайна Cloudflare (llama-8b)."""
    parsed, _ = await cloudflare.complete_json(
        build_dish_messages(name, user_message), schema=DISH_SCHEMA, max_tokens=800,
        label=f"спеки блюда: {name}",
    )
    return {
        "id": _slug(name, i),
        "name": name,
        "emoji": emoji or "🍽️",
        "servings": parsed.get("servings", 2),
        "prep_min": parsed.get("prep_min", 15),
        "cook_min": parsed.get("cook_min", 30),
        "tags": parsed.get("tags", []),
        "storage": parsed.get("storage")
        or {"vacuum": True, "freeze": True, "shelf_life_days": 30, "note": ""},
        "ingredients": parsed.get("ingredients", []),
        "steps": [],
        "tips": [],
    }


async def _validate_and_fix(dishes: list[dict], user_message: str, gate=None) -> None:
    """Валидатор (главная модель CF) даёт вердикты; плохие блюда перегенерирует спекер (8b)."""
    if not dishes:
        return
    try:
        parsed, _ = await cloudflare.complete_json(
            build_validate_messages(dishes),
            schema=VALIDATE_SCHEMA,
            model=cf_main(gate or cloudflare),
            max_tokens=500,
            label="валидатор блюд",
        )
    except Exception as exc:  # валидатор не критичен — не роняем генерацию
        logger.warning("validator skipped: %s", str(exc)[:150])
        return

    bad: list[tuple[int, list[str]]] = []
    for r in parsed.get("results", []):
        i = r.get("index")
        if isinstance(i, int) and 0 <= i < len(dishes) and not r.get("ok"):
            bad.append((i, r.get("issues", [])))
    if not bad:
        return

    logger.info("validator: перегенерируем %d блюд", len(bad))
    fixes = await asyncio.gather(
        *(
            _gen_dish(
                i,
                dishes[i].get("name", ""),
                dishes[i].get("emoji", ""),
                f"{user_message}. Исправь ингредиенты/количества: {'; '.join(issues)}",
            )
            for i, issues in bad
        ),
        return_exceptions=True,
    )
    for (i, _), fixed in zip(bad, fixes):
        if isinstance(fixed, dict):
            dishes[i] = fixed


_DEFAULT_STORAGE = {"vacuum": True, "freeze": True, "shelf_life_days": 45, "note": ""}


def _clean_dish(i: int, d: dict) -> dict:
    name = _clean_name(str(d.get("name", f"Блюдо {i + 1}")))
    return {
        "id": _slug(name, i),
        "name": name,
        "emoji": d.get("emoji") or "🍽️",
        "servings": d.get("servings", 4),
        "prep_min": d.get("prep_min", 15),
        "cook_min": d.get("cook_min", 30),
        "tags": d.get("tags", []),
        "garnish": str(d.get("garnish") or "").strip(),
        "storage": d.get("storage") or dict(_DEFAULT_STORAGE),
        "ingredients": d.get("ingredients", []),
        "steps": d.get("steps", []),
        "tips": d.get("tips", []),
    }


def _resolve_variety(avoid_titles: list[str], variety: str | None) -> str:
    """variety=None → новое серверное зерно (и запись в AI-контекст для логов);
    строка (в т.ч. пустая) — как есть (правки передают свою лёгкую подсказку)."""
    if variety is not None:
        return variety
    text, seed = _variety_hint(avoid_titles)
    set_ai_context(variety=seed)
    return text


async def generate_plan(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f",
    model: str = "", count_plan: bool = True, *,
    in_plan: list[str] | None = None, context: str = "", variety: str | None = None,
) -> dict[str, Any]:
    """План выбранной моделью. Без фолбэков: модель либо отвечает, либо кидает AIError.

    DeepSeek/Gemini — один запрос (весь план); Cloudflare — пайплайн меню→спеки→валидатор.
    count_plan=False — вызов изнутри правки (add/create), не считаем как отдельный план.
    in_plan — блюда текущего плана (для add), context — контекст беседы (исходный запрос),
    variety — None: новое серверное зерно разнообразия; строка — готовая подсказка.
    """
    gate = gate_for(model)
    if count_plan:
        enforce_daily(gate, "plan")  # дневной лимит на Claude (no-op для остальных)
    variety = _resolve_variety(avoid_titles, variety)
    if _is_cf(gate):
        return await _generate_plan_cloudflare(
            user_message, avoid_titles, count, gender,
            in_plan=in_plan, context=context, variety=variety, gate=gate,
        )

    parsed, _ = await gate.complete_json(
        build_ds_plan_messages(
            user_message, avoid_titles, count, gender,
            in_plan=in_plan, variety=variety, context=context, date_hint=_date_hint(),
        ),
        max_tokens=3000,
        temperature=_PLAN_TEMPERATURE,
        label=f"план: {_dishes_word(count)}",
    )
    dishes = [_clean_dish(i, d) for i, d in enumerate((parsed.get("dishes") or [])[:_MAX_DISHES])]
    if not dishes:
        raise AIError(f"{gate.provider} вернул пустой план")
    logger.info("plan via %s: dishes=%d", gate.provider, len(dishes))
    return {
        "reply": parsed.get("reply") or "Готово — вот план на неделю.",
        "title": _clean_title(parsed.get("title") or "План на неделю"),
        "week_label": _week_label(),
        "dishes": dishes,
        "provider": gate.provider,
    }


async def generate_plan_stream(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f",
    model: str = "", *, context: str = "",
) -> AsyncIterator[tuple[str, Any]]:
    """Потоковый план: yield ('meta', {reply,title,week_label,provider}) → ('dish', dish)…

    Стриминговые модели (DeepSeek/Gemini) отдают блюда по мере генерации; Cloudflare
    (без стрима) собирает план пайплайном и отдаёт теми же событиями. Без фолбэков —
    падение модели пробрасывается наверх (роутер отдаёт event: error).
    context — память беседы (первое сообщение + сводка, services/summary.memory)."""
    week = _week_label()
    gate = gate_for(model)
    enforce_daily(gate, "plan")  # дневной лимит на Claude (no-op для остальных)
    variety = _resolve_variety(avoid_titles, None)  # новое зерно на каждый новый план

    if gate.supports_stream:
        parser = PlanStreamParser()
        emitted = 0
        meta_sent = False
        async for delta in gate.stream_json(
            build_ds_plan_messages(
                user_message, avoid_titles, count, gender,
                variety=variety, context=context, date_hint=_date_hint(),
            ),
            max_tokens=3000,
            temperature=_PLAN_TEMPERATURE,
            label=f"план (поток): {_dishes_word(count)}",
        ):
            parser.feed(delta)
            if not meta_sent:
                meta = parser.meta()
                if meta:
                    meta_sent = True
                    yield "meta", {
                        "reply": meta["reply"] or "Готово — вот план на неделю.",
                        "title": _clean_title(meta["title"] or "План на неделю"),
                        "week_label": week,
                        "provider": gate.provider,
                    }
            for d in parser.new_dishes():
                if emitted >= _MAX_DISHES:
                    break
                if not meta_sent:
                    meta_sent = True
                    yield "meta", {
                        "reply": "Готово — вот план на неделю.",
                        "title": "План на неделю",
                        "week_label": week,
                        "provider": gate.provider,
                    }
                yield "dish", _clean_dish(emitted, d)
                emitted += 1
        if not emitted:
            raise AIError(f"{gate.provider} вернул пустой план")
        logger.info("plan stream via %s: dishes=%d", gate.provider, emitted)
        return

    # Нестриминговые гейты (Cloudflare-пайплайн, Gemini — у него стрим JSON рвётся):
    # собираем план целиком и отдаём теми же событиями. count_plan=False — лимит уже учтён выше.
    data = await generate_plan(
        user_message, avoid_titles, count, gender, model, count_plan=False, variety=variety,
        context=context,
    )
    yield "meta", {
        "reply": data["reply"],
        "title": data["title"],
        "week_label": data["week_label"],
        "provider": data["provider"],
    }
    for d in data["dishes"]:
        yield "dish", d


async def _generate_plan_cloudflare(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f", *,
    in_plan: list[str] | None = None, context: str = "", variety: str = "", gate=None,
) -> dict[str, Any]:
    """Пайплайн Cloudflare: меню (главная модель) → спеки (8b, параллельно) → валидация.
    gate — Cloudflare-гейт с моделью из настроек (меню и валидатор идут ею)."""
    gate = gate or cloudflare
    names, _ = await cloudflare.complete_json(
        build_names_messages(
            user_message, avoid_titles, count, gender,
            in_plan=in_plan, variety=variety, context=context, date_hint=_date_hint(),
        ),
        schema=NAMES_SCHEMA,
        model=cf_menu(gate),
        max_tokens=120 + count * 110,
        label="меню",
    )
    entries = (names.get("dishes") or [])[:_MAX_DISHES]  # число блюд решает модель (дефолт — count)

    dishes = list(
        await asyncio.gather(
            *(
                _gen_dish(i, _clean_name(d.get("name", f"Блюдо {i + 1}")), d.get("emoji", ""), user_message)
                for i, d in enumerate(entries)
            )
        )
    )

    await _validate_and_fix(dishes, user_message, gate)

    if not dishes:
        raise AIError("Cloudflare вернул пустой план")
    logger.info("plan via Cloudflare: dishes=%d (menu+specs+validate)", len(dishes))
    return {
        "reply": names.get("reply") or "Готово — вот план на неделю.",
        "title": _clean_title(names.get("title") or "План на неделю"),
        "week_label": _week_label(),
        "dishes": dishes,
        "provider": cloudflare.provider,
    }


async def generate_dish_detail(
    name: str, servings: int = 4, change: str = "", model: str = "", *,
    dish: dict | None = None, request: str = "", mention: str = "",
    discussion: str = "", current: str = "", regenerate: bool = False,
) -> dict:
    """Полная деталь блюда (ингредиенты + шаги + советы + note) — лениво при открытии.
    change — правка рецепта (напр. «убрать болгарский перец»): перегенерирует рецепт с учётом.
    dish — блюдо из плана (шапка: теги/тайминги/гарнир), request — исходный запрос беседы:
    mention — реплика к плану, где упомянуто блюдо. Всё — чтобы рецепт не расходился
    с тем, что обещано в плане. discussion/current/regenerate — «↻ Перегенерировать» и правка
    из обсуждения: реплики обсуждения рецепта, выжимка текущего варианта, правило перегенерации.

    Генерит выбранная модель; пусто → модель рецептов по умолчанию из настроек.
    Без фолбэков — падение пробрасывается наверх."""
    gate = gate_for(model, "recipe")
    enforce_daily(gate, "recipe")  # дневной лимит на Claude (no-op для остальных)
    label = (
        f"деталь блюда: {name}" + (" [перегенерация]" if regenerate else "")
        + (f" ({change})" if change else "")
    )
    messages = build_dish_detail_messages(
        name, servings, change, dish=dish, request=request, mention=mention,
        discussion=discussion, current=current, regenerate=regenerate,
    )
    if _is_cf(gate):
        parsed, _ = await gate.complete_json(
            messages, schema=DISH_DETAIL_SCHEMA, model=cf_main(gate),
            max_tokens=3000, label=label,
        )
    else:
        parsed, _ = await gate.complete_json(messages, max_tokens=3000, label=label)
    return _clean_detail(parsed, gate.provider)


def _clean_detail(parsed: dict, provider: str = "") -> dict:
    return {
        "ingredients": parsed.get("ingredients") or [],
        "steps": parsed.get("steps") or [],
        "tips": parsed.get("tips") or [],
        "note": (parsed.get("note") or "").strip(),
        "provider": provider,
    }


def _clean_cook_steps(steps: list) -> list[dict]:
    """Нормализуем шаги плана готовки: приводим типы, order — с фолбэком на индекс."""
    out: list[dict] = []
    for i, s in enumerate(steps or []):
        if not isinstance(s, dict):
            continue
        try:
            order = int(s.get("order"))
        except (TypeError, ValueError):
            order = i + 1
        try:
            active = int(s.get("active_min", s.get("activeMin", 0)) or 0)
        except (TypeError, ValueError):
            active = 0
        try:
            passive = int(s.get("passive_min", s.get("passiveMin", 0)) or 0)
        except (TypeError, ValueError):
            passive = 0
        dishes = s.get("dishes") or []
        out.append({
            "order": order,
            "phase": str(s.get("phase", "") or ""),
            "text": str(s.get("text", "") or ""),
            "active_min": active,
            "passive_min": passive,
            "dishes": [str(x) for x in dishes if x],
        })
    return out


async def generate_cooking_plan(
    dishes: list[dict], model: str = "", *, discussion: str = "", regenerate: bool = False
) -> dict:
    """Единый оптимизированный план готовки по ВСЕМ блюдам недели — лениво, кэш в плане.
    discussion/regenerate — «↻ Перегенерировать»: учесть обсуждение плана готовки в чате.

    Генерит выбранная модель; пусто → модель плана готовки по умолчанию из настроек.
    Без фолбэков — падение пробрасывается наверх."""
    gate = gate_for(model, "cooking")
    enforce_daily(gate, "recipe")  # дневной лимит на Claude (no-op для остальных)
    label = f"план готовки: {len(dishes)} блюд" + (" [перегенерация]" if regenerate else "")
    messages = build_cook_plan_messages(dishes, discussion=discussion, regenerate=regenerate)
    # План по всем блюдам длинный: 3000 токенов на 4 блюда обрезало JSON (Claude/DeepSeek).
    # Даём ~1500 на блюдо сверху базы, потолок 8000 (максимум вывода deepseek-chat — 8192).
    max_tokens = min(8000, 2000 + 1500 * len(dishes))
    if _is_cf(gate):
        parsed, _ = await gate.complete_json(
            messages, schema=COOKPLAN_SCHEMA, model=cf_main(gate),
            max_tokens=max_tokens, label=label,
        )
    else:
        parsed, _ = await gate.complete_json(messages, max_tokens=max_tokens, label=label)
    return {
        "steps": _clean_cook_steps(parsed.get("steps") or []),
        "note": (parsed.get("note") or "").strip(),
        "provider": gate.provider,
    }


def _dish_names(dishes: list[dict]) -> list[str]:
    return [str(d.get("name", "")) for d in dishes if d.get("name")]


def _match_index(dishes: list[dict], name: str) -> int | None:
    """Индекс блюда, лучше всего совпадающего с name (fuzzy). None — если не нашли."""
    name = (name or "").strip().lower()
    if not name:
        return None
    names = [str(d.get("name", "")).lower() for d in dishes]
    for i, n in enumerate(names):  # точное/подстрочное совпадение — в приоритете
        if n == name or name in n or n in name:
            return i
    close = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
    return names.index(close[0]) if close else None


def _reid(dish: dict, i: int, existing_ids: set[str]) -> dict:
    """Присваивает блюду уникальный id, не конфликтующий с existing_ids."""
    base = _slug(dish.get("name", "блюдо"), i)
    new_id, k = base, i
    while new_id in existing_ids:
        k += 1
        new_id = _slug(dish.get("name", "блюдо"), k)
    dish = {**dish, "id": new_id}
    existing_ids.add(new_id)
    return dish


async def _edit_actions(
    gate, title: str, dishes: list[dict], user_message: str, context: str = ""
) -> tuple[list[dict[str, Any]], str]:
    """Правки без tools API (Gemini/Cloudflare): structured actions → формат tool_calls."""
    parsed, _ = await gate.complete_json(
        build_edit_action_messages(title, _dish_names(dishes), user_message, context),
        schema=EDIT_ACTION_SCHEMA,
        model=(cf_main(gate) if _is_cf(gate) else None),
        max_tokens=500,
        label="правка плана (actions)",
    )
    op_map = {
        "add": "add_dishes",
        "remove": "remove_dish",
        "replace": "replace_dish",
        "edit": "edit_dish",
        "create": "create_plan",
    }
    calls: list[dict[str, Any]] = []
    for a in parsed.get("actions") or []:
        op = op_map.get(str(a.get("op", "")).lower())
        if not op:
            continue
        calls.append({
            "name": op,
            "args": {
                "query": a.get("query", ""),
                "note": a.get("query", ""),
                "name": a.get("name", ""),
                "old_name": a.get("name", ""),
                "change": a.get("change", "") or a.get("query", ""),
                "count": a.get("count", 1),
            },
        })
    return calls, parsed.get("reply", "")


def _pick_one(candidates: list[dict], query: str, exclude: set[str]) -> dict | None:
    """Ровно одно блюдо из ответа модели. Если модель вернула не одно (бывало 5–7 на
    замену) — берём лучшее по совпадению с пожеланием (иначе первое не из exclude) и пишем
    предупреждение. Лишние блюда в план НЕ попадают никогда."""
    cands = [c for c in candidates if isinstance(c, dict) and c.get("name")]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    logger.warning("single dish: модель вернула %d блюд вместо 1 — выбираем одно", len(cands))
    fresh = [c for c in cands if _norm_text(str(c.get("name"))) not in exclude] or cands
    q = _norm_text(query).strip()
    if not q:
        return fresh[0]

    def score(c: dict) -> float:
        text = _norm_text(" ".join([str(c.get("name", ""))] + [str(t) for t in c.get("tags") or []]))
        words = [w for w in re.findall(r"[а-яa-z0-9]+", q) if len(w) >= 3]
        # грубый стемминг по началу слова: «рыбы» → «рыб», «курицей» → «кури»
        hits = sum(1 for w in words if (w[:4] if len(w) >= 5 else w[:3]) in text)
        return hits + difflib.SequenceMatcher(None, q, text).ratio()

    return max(fresh, key=score)


async def generate_single_dish(
    query: str,
    *,
    model: str = "",
    gender: str = "f",
    old_dish: dict | None = None,
    plan_dishes: list[dict] | None = None,
    rejected: list[str] | None = None,
    avoid_titles: list[str] | None = None,
    context: str = "",
) -> dict[str, Any] | None:
    """Одно блюдо для замены (old_dish задан) или добавления — отдельным промптом.

    Контекст: пожелание пользователя, контекст беседы (исходный запрос + последние реплики),
    заменяемое блюдо (название + теги), остальные блюда плана, отвергнутое в этой беседе,
    общая история. Возвращает {"dish", "reply", "provider"} или None, если блюда нет."""
    gate = gate_for(model)
    messages = build_single_dish_messages(
        query, old_dish=old_dish, plan_dishes=plan_dishes, rejected=rejected,
        avoid_titles=avoid_titles, context=context, gender=gender,
    )
    label = (
        f"замена блюда: {old_dish.get('name')}" if old_dish else "добавление блюда"
    )
    if _is_cf(gate):
        parsed, _ = await gate.complete_json(
            messages, schema=SINGLE_DISH_SCHEMA, model=cf_menu(gate),
            max_tokens=400, label=label,
        )
    else:
        parsed, _ = await gate.complete_json(messages, max_tokens=600, label=label)

    raw = parsed.get("dish")
    candidates = [raw] if isinstance(raw, dict) else list(raw or []) if isinstance(raw, list) else []
    if not candidates:  # модель ответила в формате плана — {"dishes": [...]}
        candidates = list(parsed.get("dishes") or [])
    exclude = {_norm_text(str(d.get("name", ""))) for d in (plan_dishes or [])}
    if old_dish:
        exclude.add(_norm_text(str(old_dish.get("name", ""))))
    picked = _pick_one(candidates, query, exclude)
    if picked is None:
        return None
    return {
        "dish": _clean_dish(0, picked),
        "reply": str(parsed.get("reply") or "").strip(),
        "provider": gate.provider,
    }


async def edit_plan(
    dishes: list[dict], title: str, user_message: str, gender: str = "f", model: str = "",
    context: str = "", *, avoid: list[str] | None = None, rejected: list[str] | None = None,
) -> dict[str, Any]:
    """Правит существующий план по просьбе выбранной моделью.

    DeepSeek — через function calling; Gemini/Cloudflare — через structured actions.
    Подоперации (add/replace/edit/create) идут той же моделью. Без фолбэков.
    context — узкая история диалога (исходный запрос + пара реплик), чтобы точнее понять,
    какое блюдо имеется в виду; в пограничных случаях модель задаёт уточняющий вопрос.
    avoid — «недавно ели или отвергли» (history.variety_avoid), rejected — отвергнутое
    в этой беседе (history.conversation_rejected): для add/replace/create."""
    gate = gate_for(model)
    work = [dict(d) for d in dishes]
    avoid = avoid or []
    rejected = list(rejected or [])

    if gate.supports_tools:
        calls, reply_hint = await gate.call_tools(
            build_edit_messages(title, _dish_names(work), user_message, gender, context),
            PLAN_TOOLS,
            label="правка плана (tools)",
        )
    else:
        calls, reply_hint = await _edit_actions(gate, title, work, user_message, context)

    changed: list[str] = []
    new_title = title

    for call in calls:
        op, args = call.get("name", ""), call.get("args", {})
        if op == "remove_dish":
            idx = _match_index(work, args.get("name", ""))
            if idx is not None:
                changed.append(f"убрано «{work[idx].get('name')}»")
                rejected.append(str(work[idx].get("name", "")))
                work.pop(idx)
        elif op == "add_dishes":
            cnt = max(1, min(int(args.get("count", 1) or 1), 6))
            ids = {d["id"] for d in work}
            if cnt == 1:
                one = await generate_single_dish(
                    args.get("query", ""), model=model, gender=gender, plan_dishes=work,
                    rejected=rejected, avoid_titles=avoid, context=context,
                )
                new = [one["dish"]] if one else []
            else:
                gen = await generate_plan(
                    args.get("query", ""), avoid, cnt, gender, model, count_plan=False,
                    in_plan=_dish_names(work), context=context, variety=_NEIGHBOR_HINT,
                )
                new = gen["dishes"][:cnt]
            for j, d in enumerate(new):
                nd = _reid(d, len(work) + j, ids)
                work.append(nd)
                changed.append(f"добавлено «{nd.get('name')}»")
        elif op == "replace_dish":
            idx = _match_index(work, args.get("old_name", ""))
            old = work[idx] if idx is not None else None
            others = [d for i, d in enumerate(work) if i != idx]
            one = await generate_single_dish(
                args.get("query", ""), model=model, gender=gender, old_dish=old,
                plan_dishes=others, rejected=rejected, avoid_titles=avoid, context=context,
            )
            if one:
                ids = {d["id"] for d in work}
                nd = _reid(one["dish"], (idx if idx is not None else len(work)), ids)
                if idx is not None:
                    work[idx] = nd
                    rejected.append(str(old.get("name", "")))
                else:
                    work.append(nd)
                changed.append(
                    f"«{old.get('name')}» заменено на «{nd.get('name')}»"
                    if old else f"добавлено «{nd.get('name')}»"
                )
        elif op == "edit_dish":
            idx = _match_index(work, args.get("name", ""))
            change = args.get("change", "")
            if idx is not None and change:
                dish = work[idx]
                detail = await generate_dish_detail(
                    dish.get("name", ""), dish.get("servings", 4), change, model, dish=dish
                )
                # Пишем в варианты (variants[модель] + active_model), а не только в плоские
                # поля: иначе при следующем открытии рецепт брался из старого варианта и
                # правка «терялась».
                work[idx] = with_detail(dish, gate.key, detail)
                changed.append(f"рецепт «{dish.get('name')}» обновлён ({change})")
        elif op == "create_plan":
            # Пересборка — новое меню: исходный запрос (в context) и история avoid сохраняются,
            # зерно разнообразия — новое (variety=None).
            cnt = max(2, min(int(args.get("count") or len(work) or 5), 12))
            gen = await generate_plan(
                args.get("note", user_message), avoid + _dish_names(work), cnt, gender, model,
                count_plan=False, context=context,
            )
            work = gen["dishes"]
            new_title = gen.get("title", title)
            changed = ["меню пересобрано"]

    if changed:
        reply = "Готово: " + ", ".join(changed) + "."
    else:
        reply = reply_hint.strip() or "Не понятно, что изменить в плане. Уточните?"

    logger.info("plan edited: ops=%d changed=%d provider=%s", len(calls), len(changed), gate.provider)
    return {
        "reply": reply,
        "title": new_title,
        "dishes": work,
        "provider": gate.provider,
        "changed": changed,
    }


async def replace_dish_by_id(
    dishes: list[dict], title: str, dish_id: str, query: str, gender: str = "f", model: str = "",
    *, context: str = "", rejected: list[str] | None = None, avoid: list[str] | None = None,
) -> dict[str, Any]:
    """Точечная замена конкретного блюда (кнопка «заменить» в карточке): без выбора функции
    моделью — сразу генерим замену отдельным промптом на одно блюдо. query — пожелание
    пользователя (может быть пустым); context/rejected/avoid — память беседы и история."""
    gate = gate_for(model)
    work = [dict(d) for d in dishes]
    idx = next((i for i, d in enumerate(work) if d.get("id") == dish_id), None)
    if idx is None:
        idx = _match_index(work, dish_id)  # запасной путь: трактуем как название
    if idx is None:
        return {"reply": "Не нашлось блюдо для замены.", "title": title,
                "dishes": dishes, "provider": gate.provider, "changed": []}

    old = work[idx]
    others = [d for i, d in enumerate(work) if i != idx]
    one = await generate_single_dish(
        query, model=model, gender=gender, old_dish=old, plan_dishes=others,
        rejected=rejected, avoid_titles=avoid, context=context,
    )
    if not one:
        return {"reply": "Не удалось подобрать замену. Попробуйте ещё раз.", "title": title,
                "dishes": dishes, "provider": gate.provider, "changed": []}

    ids = {d["id"] for d in work}
    nd = _reid(one["dish"], idx, ids)
    work[idx] = nd
    changed = [f"«{old.get('name', '')}» заменено на «{nd.get('name')}»"]
    return {"reply": "Готово: " + changed[0] + ".", "title": title,
            "dishes": work, "provider": gate.provider, "changed": changed}


def remove_dish_by_id(dishes: list[dict], title: str, dish_id: str) -> dict[str, Any]:
    """Детерминированное удаление блюда (крестик) — БЕЗ модели, только правка списка."""
    work = [dict(d) for d in dishes]
    idx = next((i for i, d in enumerate(work) if d.get("id") == dish_id), None)
    if idx is None:
        idx = _match_index(work, dish_id)
    if idx is None:
        return {"reply": "Не нашлось блюдо для удаления.", "title": title,
                "dishes": dishes, "provider": "", "changed": []}
    name = work.pop(idx).get("name", "")
    changed = [f"убрано «{name}»"]
    return {"reply": "Готово: " + changed[0] + ".", "title": title,
            "dishes": work, "provider": "", "changed": changed}


async def add_dish_direct(
    dishes: list[dict], title: str, query: str, gender: str = "f", model: str = "",
    *, context: str = "", rejected: list[str] | None = None, avoid: list[str] | None = None,
) -> dict[str, Any]:
    """Добавить одно блюдо в существующий план (кнопка «Добавить блюдо») — без выбора функции
    моделью. query — пожелание пользователя (может быть пустым)."""
    gate = gate_for(model)
    work = [dict(d) for d in dishes]
    one = await generate_single_dish(
        query, model=model, gender=gender, plan_dishes=work, rejected=rejected,
        avoid_titles=avoid, context=context,
    )
    if not one:
        return {"reply": "Не удалось подобрать блюдо. Попробуйте ещё раз.", "title": title,
                "dishes": dishes, "provider": gate.provider, "changed": []}
    ids = {d["id"] for d in work}
    nd = _reid(one["dish"], len(work), ids)
    work.append(nd)
    changed = [f"добавлено «{nd.get('name')}»"]
    return {"reply": "Готово: " + changed[0] + ".", "title": title,
            "dishes": work, "provider": gate.provider, "changed": changed}


# Нормализация списка покупок: выход ≈ 35 токенов на позицию. Большой список режем на куски
# по категориям (синонимы обычно в одной категории), чтобы не обрезало по max_tokens.
_SHOP_TOKENS_PER_ITEM = 40
_SHOP_CHUNK = 80


def _shop_max_tokens(n: int) -> int:
    return min(200 + _SHOP_TOKENS_PER_ITEM * n, 4000)


def _shop_chunks(items: list[dict]) -> list[list[dict]]:
    if len(items) <= _SHOP_CHUNK:
        return [items]
    by_cat: dict[str, list[dict]] = {}
    for it in items:
        by_cat.setdefault(str(it.get("category") or "Прочее"), []).append(it)
    chunks: list[list[dict]] = [[]]
    for group in by_cat.values():
        for it in group:
            if len(chunks[-1]) >= _SHOP_CHUNK:
                chunks.append([])
            chunks[-1].append(it)
    return [c for c in chunks if c]


async def normalize_shopping(
    items: list[dict], discussion: str = "", model: str = ""
) -> list[dict]:
    """Доводит детерминированную базу списка покупок выбранной моделью.

    model — ключ модели; пусто → модель списка покупок по умолчанию из настроек (изначально
    Cloudflare mistral). Cloudflare — со строгой json_schema и mistral-24b, остальные
    (DeepSeek/Gemini/Claude) — JSON-режим, форма ответа описана в SHOP_SYSTEM.
    Падение/пустой ответ — пробрасываем AIError: GET вернёт базу и НЕ закэширует её
    под подписью (чтобы следующий заход попробовал нормализовать снова), перегенерация — 502.
    discussion — «↻ Перегенерировать»: пожелания из обсуждения списка покупок в чате."""
    if not items:
        return []
    gate = gate_for(model, "shopping")
    # У Cloudflare — отдельная модель (mistral) и схема; у остальных — дефолтная модель гейта.
    cf_kw = (
        {"schema": SHOP_SCHEMA, "model": cf_main(gate)} if _is_cf(gate) else {}
    )
    chunks = _shop_chunks(items)
    results = await asyncio.gather(*(
        gate.complete_json(
            build_shop_normalize_messages(chunk, discussion),
            **cf_kw,
            max_tokens=_shop_max_tokens(len(chunk)),
            label="список покупок (нормализация)"
            + (" [перегенерация]" if discussion else "")
            + (f" {i + 1}/{len(chunks)}" if len(chunks) > 1 else ""),
        )
        for i, chunk in enumerate(chunks)
    ))
    out: list[dict] = []
    for parsed, _ in results:
        got = parsed.get("items") or []
        if not got:
            raise AIError(f"{gate.provider} вернул пустой список покупок")
        out.extend(got)
    return out


# --- Обсуждение цели в чате («💬 Обсудить в чате») ---

# Функции DeepSeek → операции обсуждения (как у structured-ответа остальных моделей).
_DISCUSS_OPS = {"update_recipe": "edit", "replace_dish": "replace", "regenerate": "regenerate"}
# Какие операции допустимы для цели: у рецепта — правка/замена, у готовки/покупок — пересборка.
_DISCUSS_ALLOWED = {
    "recipe": {"edit", "replace"},
    "cooking": {"regenerate"},
    "shopping": {"regenerate"},
}


async def discuss_reply(
    target: str,
    context: str,
    turns: list[dict[str, str]],
    question: str,
    gender: str = "f",
    model: str = "",
) -> dict[str, Any]:
    """Ответ в обсуждении цели (рецепт / план готовки / список покупок) выбранной моделью.

    DeepSeek — function calling с маленьким набором функций на цель; Gemini/Claude/Cloudflare —
    structured JSON (DISCUSS_SCHEMA, Cloudflare — с json_schema). Возвращает
    {"reply", "op": none|edit|replace|regenerate, "change", "query", "provider"}.
    Сам ничего не меняет — применяет роутер. Без фолбэков: AIError пробрасываем."""
    gate = gate_for(model)
    label = f"обсуждение: {target}"
    op, change, query, reply = "none", "", "", ""
    if gate.supports_tools:
        calls, reply = await gate.call_tools(
            build_discuss_messages(target, context, turns, question, gender, tools=True),
            DISCUSS_TOOLS.get(target, []),
            max_tokens=900,
            label=label + " (tools)",
        )
        for call in calls:
            got = _DISCUSS_OPS.get(call.get("name", ""))
            if got:
                args = call.get("args") or {}
                op, change, query = got, str(args.get("change", "")), str(args.get("query", ""))
                break
    else:
        parsed, _ = await gate.complete_json(
            build_discuss_messages(target, context, turns, question, gender),
            schema=DISCUSS_SCHEMA,
            model=(cf_main(gate) if _is_cf(gate) else None),
            max_tokens=1200,
            label=label,
        )
        reply = str(parsed.get("reply") or "")
        action = parsed.get("action") or {}
        if isinstance(action, dict):
            op = str(action.get("op") or "none").lower()
            change = str(action.get("change") or "")
            query = str(action.get("query") or "")
    if op not in _DISCUSS_ALLOWED.get(target, set()):
        op = "none"
    if op == "edit" and not change.strip():
        change = question.strip()  # модель не описала правку — берём саму просьбу
    return {
        "reply": reply.strip(),
        "op": op,
        "change": change.strip(),
        "query": query.strip(),
        "provider": gate.provider,
    }
