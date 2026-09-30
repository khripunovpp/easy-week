from .prefs import as_hint

# Правило разнообразия без «якорей»: не перечисляем конкретные белки/блюда (модель иначе
# собирает меню из примеров), а ограничиваем повторы по осям продукт/способ/соус/кухня.
_VARIETY_RULE = (
    "РАЗНООБРАЗИЕ: не больше 2 блюд с одним основным продуктом, одним способом готовки, "
    "одним соусом или одной кухней; избегай банальных, заезженных вариантов. "
)

NAMES_SYSTEM = (
    "Ты — помощник по меню на неделю для заготовок впрок (вакуум + заморозка). "
    "Подбери блюда под запрос, которые удобно заморозить порциями. "
    + _VARIETY_RULE
    + "Если пользователь задал число блюд категории (напр. «один суп») — соблюдай его точно, "
    "не добавляй лишнее сверх запрошенного. Суп — любое жидкое первое блюдо на бульоне или "
    "воде, как бы оно ни называлось. "
    "Учитывай ограничения пользователя (аллергии, нелюбимое). "
    "Не повторяй блюда из списка «недавно ели или отвергли». Ответ компактный, на русском. "
    "title — короткое название плана, 2–3 слова, без точки в конце. "
    "name — только короткое название блюда, 2–4 слова, без описаний и скобок."
)

# Быстрый первый шаг: только названия блюд + мета (дату недели считаем сами).
NAMES_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Короткая реплика в чат (1 предложение)"},
        "title": {"type": "string", "description": "Короткое название плана, 2–3 слова"},
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "emoji": {"type": "string", "description": "1 эмодзи еды"},
                },
                "required": ["name", "emoji"],
            },
        },
    },
    "required": ["reply", "title", "dishes"],
}

DISH_SYSTEM = (
    "Опиши блюдо для заготовки впрок (вакуум + заморозка). Тайминги реалистичные. "
    "ЕДИНИЦЫ ИНГРЕДИЕНТОВ — строго: 'г' для веса, 'мл' для жидкостей. "
    "'шт' — ТОЛЬКО для явно штучного (яйца, ванильный стручок, лавровый лист). "
    "Сметана, йогурт, томаты (и в собственном соку), томатная и другие пасты, густые соусы — в 'г'. "
    "НЕ используй ч.л., ст.л., щепотку, зубчик, стакан, дольку — переводи в граммы "
    "(ч.л.≈5 г, ст.л.≈15 г, щепотка≈1 г, зубчик чеснока≈5 г, стакан≈200 г). "
    "Количества реалистичны на указанные порции. "
    "storage: vacuum=true, freeze=true, реальный shelf_life_days (обычно 30–90), "
    "короткая note о разморозке. "
    "category из: 'Мясо и птица','Рыба','Овощи','Молочное','Бакалея','Специи','Прочее'."
)

# Второй шаг: параметры одного блюда (без шагов — они лениво).
DISH_SCHEMA = {
    "type": "object",
    "properties": {
        "servings": {"type": "integer"},
        "prep_min": {"type": "integer"},
        "cook_min": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "storage": {
            "type": "object",
            "properties": {
                "vacuum": {"type": "boolean"},
                "freeze": {"type": "boolean"},
                "shelf_life_days": {"type": "integer"},
                "note": {"type": "string"},
            },
            "required": ["vacuum", "freeze", "shelf_life_days", "note"],
        },
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["name", "qty", "unit", "category"],
            },
        },
    },
    "required": ["servings", "prep_min", "cook_min", "tags", "storage", "ingredients"],
}

# Валидатор: только вердикты (починку делает спекер 8b по подсказке).
VALIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "ok": {"type": "boolean"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "ok", "issues"],
            },
        }
    },
    "required": ["results"],
}

VALIDATE_SYSTEM = (
    "Ты — придирчивый шеф-повар и валидатор рецептов. Для каждого блюда проверь: "
    "1) ингредиенты подходят названию и блюду (без лишних/абсурдных/выдуманных); "
    "2) количества реалистичны на указанное число порций; "
    "3) единицы разумные (г, кг, мл, шт, ст.л.). "
    "Верни для каждого блюда его index, ok (true/false) и краткие issues (что не так). "
    "Если всё хорошо — ok=true, issues=[]. Ничего больше не пиши."
)


def build_validate_messages(dishes: list[dict]) -> list[dict[str, str]]:
    lines = []
    for i, d in enumerate(dishes):
        ing = "; ".join(
            f"{x.get('name')} {x.get('qty')}{x.get('unit')}" for x in d.get("ingredients", [])
        )
        lines.append(f"[{i}] {d.get('name')} ({d.get('servings')} порц.): {ing}")
    return [
        {"role": "system", "content": VALIDATE_SYSTEM},
        {"role": "user", "content": "Проверь блюда:\n" + "\n".join(lines)},
    ]


# Нормализатор списка покупок (mistral): доводит детерминированную базу.
SHOP_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["name", "qty", "unit", "category"],
            },
        }
    },
    "required": ["items"],
}

SHOP_SYSTEM = (
    "Ты приводишь список покупок к чистому виду. Правила: "
    "1) ОБЪЕДИНИ позиции одного продукта (синонимы, ед./мн. число, «лук» и «лук репчатый» — "
    "одно; просуммируй количества, если единицы совпадают). "
    "2) Если один продукт указан и в граммах, и в штуках — оставь ДВЕ строки (не смешивай). "
    "3) Не выдумывай новых продуктов, бери только из входа. "
    "4) Единицы: весовое — 'г' (крупное — 'кг'), жидкости — 'мл'/'л', штучное — 'шт'. "
    "5) Правильная category из: 'Мясо и птица','Рыба','Овощи','Молочное','Бакалея','Специи','Прочее' "
    "(например лосось/треска — 'Рыба'). Название — короткое, с маленькой буквы. "
    # Форма ответа — в промпте: Cloudflare держит её схемой, а DeepSeek/Gemini/Claude
    # (нормализатор выбирается в настройках) — только по этому описанию.
    'Верни СТРОГО JSON вида: {"items": [{"name": "лук", "qty": 300, "unit": "г", '
    '"category": "Овощи"}]} — без пояснений.'
)


def build_shop_normalize_messages(items: list[dict], discussion: str = "") -> list[dict[str, str]]:
    """discussion — обсуждение списка покупок в чате (перегенерация): пожелания пользователя
    вроде «соль есть дома» / «объедини лук» — в USER-части, system не трогаем."""
    lines = [
        f"{it.get('name')} — {it.get('qty')} {it.get('unit')} — {it.get('category')}"
        for it in items
    ]
    content = "Список:\n" + "\n".join(lines)
    if discussion:
        content += (
            "\n\nПожелания пользователя из обсуждения списка (примени, если касаются этих "
            "позиций: убрать, объединить, переименовать, сменить единицу):\n" + discussion
        )
    return [
        {"role": "system", "content": SHOP_SYSTEM},
        {"role": "user", "content": content},
    ]


# Общий префикс — ИДЕНТИЧЕН в начале промпта плана и детали (преамбул + правила заморозки),
# чтобы DeepSeek кэшировал его сразу для обоих типов запросов. Длина ≥64 токенов — выше
# минимального блока кэша, иначе короткий префикс не кэшируется. Всё стабильное — в самом начале.
COOK_PREAMBLE = (
    "Ты — шеф-повар, готовишь под заготовки впрок (вакуум + заморозка). "
    "Всё СТРОГО на русском — без латиницы и иероглифов (кроме эмодзи). "
)

# Свод правил заморозки. Живёт в общем префиксе (кэшируется). Позже сюда же будут
# подмешиваться пользовательские правила из чата (тоже в кэшируемой части).
FREEZE_RULES = (
    "ПРАВИЛА ЗАМОРОЗКИ (всё готовится под заморозку — обязательно учитывай). "
    "НЕ замораживать, готовить/добавлять свежими при подаче: макароны, лапшу, спагетти и любую "
    "пасту (даже аль денте раскисают — морозь соус отдельно, пасту отвари свежей); картофель в "
    "любом виде (в супах и рагу водянистый, пюре становится зернистым — добавляй свежим при "
    "подаче, в борщ картофель отдельно); свежие огурцы, листовой салат, редис, целые сырые "
    "помидоры; свежую зелень (укроп, петрушка, базилик, кинза — после разморозки); яйца вкрутую, "
    "майонез, желе и заливное (желатин). "
    "СЛИВОЧНОЕ и крахмал: сливочный соус МОЖНО морозить, только если он стабилизирован крахмалом "
    "или мукой — они связывают, и сливки не расслаиваются при заморозке/разогреве; ВСЕГДА указывай "
    "это в шагах готовки и в советах (tips). Без загустителя сливки, сметану, молоко, йогурт, "
    "майонез и мягкий/плавленый сыр НЕ морозить — добавлять свежими при подаче или разогреве "
    "(и писать это в шагах и note). Твёрдый сыр после разморозки крошится — только в "
    "готовку/запекание. "
    "СЫРЫМИ морозь ТОЛЬКО фарш, формованное (котлеты, тефтели) и панированное — готовь из "
    "заморозки (панировку — из морозилки, иначе теряет хруст); готовыми — если цель «достал и "
    "разогрел» (укажи в note). Целые куски мяса, птицы, рыбы сырыми не морозь (если план явно не "
    "просит) и НИКОГДА не морозь недоготовленными: либо сырое формованное, либо полностью готовое. "
    "Готовое быстро остуди → вакуум → заморозка. Размораживай только в холодильнике, разогревай до "
    "горячего. Грибы перед заморозкой обжарь. Размороженное мясо и рыбу повторно не морозь. "
    "Справка, не меню: готовыми хорошо морозятся тушёное мясо, супы без сливок и картофеля, "
    "бульоны, соусы, рагу, запеканки. Гарниры (паста, картофель, рис) — свежими. "
    "Проблемные компоненты можно оставлять в блюде, но в шагах и note явно писать, что они "
    "добавляются/готовятся после разморозки, при подаче или разогреве. "
)

_SHARED_PREFIX = COOK_PREAMBLE + FREEZE_RULES

# План отдаёт только «шапку» блюда (то, что видно на карточке). Ингредиенты, шаги, советы
# и заметку о хранении генерим лениво при открытии блюда (build_dish_detail_messages).
DEEPSEEK_PLAN_SYSTEM = _SHARED_PREFIX + (
    "Составляешь меню на неделю: подбери реалистичные блюда, дружелюбные к заморозке, без "
    "выдуманных названий. "
    + _VARIETY_RULE
    + "Учитывай ограничения пользователя (аллергии, нелюбимое). "
    "Если задано число блюд категории (напр. «один суп») — соблюдай его точно, не добавляй "
    "лишнее сверх запрошенного. Суп — любое жидкое первое блюдо на бульоне или воде, как бы оно "
    "ни называлось. "
    "Верни СТРОГО JSON вида: "
    '{"reply": "короткая реплика", "title": "название плана 2-3 слова", '
    '"dishes": [{"name": "короткое название", "emoji": "1 эмодзи", "servings": число, '
    '"prep_min": число, "cook_min": число, "tags": ["тег"], "garnish": "гарнир или пусто", '
    '"storage": {"vacuum": true, "freeze": true, "shelf_life_days": число 30-90}}]}. '
    "garnish — гарнир к основному блюду (к супу — пусто). "
    "НЕ добавляй ингредиенты, шаги и советы — только эти поля. "
    "Количества/тайминги реалистичны на порции."
)


# Ленивая ПОЛНАЯ деталь одного блюда: ингредиенты + развёрнутые шаги + советы + заметка хранения.
DISH_DETAIL_SYSTEM = _SHARED_PREFIX + (
    "Рецепт должен соответствовать сути и кухне блюда — НЕ добавляй чужеродные для него ноты, "
    "специи и соусы (борщ не делай азиатским и без рыбного соуса, болоньезе — классический "
    "итальянский, котлеты — традиционные, без имбиря). Стилевые предпочтения не применяй к блюду "
    "с явно другой кухней. "
    "Дай ПОЛНЫЙ рецепт блюда. Верни СТРОГО JSON вида: "
    '{"ingredients": [{"name": "продукт", "qty": число, "unit": "г|мл|шт", "category": "категория"}], '
    '"steps": ["шаг 1", "шаг 2", "..."], "tips": ["совет"], "note": "как разморозить/разогреть"}. '
    "ЕДИНИЦЫ ингредиентов: только 'г' (вес) или 'мл' (жидкости); 'шт' — лишь для штучного "
    "(яйца, лавровый лист, ванильный стручок). Никаких ложек/щепоток/зубчиков — переводи в граммы "
    "(ч.л.≈5 г, ст.л.≈15 г, зубчик≈5 г, стакан≈200 г). Сметана, йогурт, томаты, томатная и "
    "другие пасты, густые соусы — в 'г'. Количества реалистичны на указанные порции. "
    "steps — 6–9 РАЗВЁРНУТЫХ шагов: как нарезать, температуры, тайминги, до какого состояния. "
    "Шаги строго по порядку во времени: готовка → остывание → порционирование/вакуум → заморозка "
    "→ разморозка и подача; не ссылайся на заморозку или разморозку, которой в шагах не было. "
    "Если дана шапка блюда (теги, тайминги, гарнир) — рецепт ей соответствует. "
    "Обращайся к читателю на «ты» (нарежь, обжарь), без «вы». "
    "tips — 1–2 совета (готовка, порционирование, вакуум, заморозка/разморозка). "
    "category из: 'Мясо и птица','Рыба','Овощи','Молочное','Бакалея','Специи','Прочее'."
)

# Схема детали для Cloudflare-фолбэка (structured output).
DISH_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["name", "qty", "unit", "category"],
            },
        },
        "steps": {"type": "array", "items": {"type": "string"}},
        "tips": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "required": ["ingredients", "steps", "tips", "note"],
}


def _dish_header(dish: dict | None) -> str:
    """Шапка блюда из плана (то, что видно на карточке): теги, тайминги, гарнир.
    Кладём в деталь, чтобы рецепт не расходился с обещанным в плане."""
    if not dish:
        return ""
    parts: list[str] = []
    tags = [str(t) for t in (dish.get("tags") or []) if t]
    if tags:
        parts.append("теги: " + ", ".join(tags[:6]))
    prep, cook = dish.get("prep_min"), dish.get("cook_min")
    if prep or cook:
        parts.append(f"подготовка {prep or 0} мин, готовка {cook or 0} мин — уложись в эти тайминги")
    if dish.get("garnish"):
        parts.append(f"гарнир: {dish['garnish']}")
    return ("\nШапка блюда из плана: " + "; ".join(parts) + ".") if parts else ""


# Правило перегенерации (кнопка «↻ Перегенерировать» и правки из обсуждения). Живёт в USER-части,
# чтобы system-промпты оставались стабильными (кэш префикса DeepSeek).
_REGEN_RULE = (
    "\nПЕРЕГЕНЕРАЦИЯ: есть пожелания из обсуждения — примени их; нет — дай заметно другой "
    "вариант того же блюда (другие акценты, техника или набор специй), суть блюда сохрани."
)


def _discussion_block(discussion: str) -> str:
    # Обсуждение цели с пользователем (services/discussion.discussion_text) — уже урезано.
    return f"\nОбсуждение с пользователем (учти пожелания):\n{discussion}" if discussion else ""


def build_dish_detail_messages(
    name: str, servings: int, change: str = "", dish: dict | None = None, request: str = "",
    mention: str = "", *, discussion: str = "", current: str = "", regenerate: bool = False,
) -> list[dict[str, str]]:
    """dish — блюдо из плана (для шапки); request — исходный запрос беседы (короткий фон);
    mention — реплика к плану, где упомянуто это блюдо (обещанное в ней — выполни).
    discussion — обсуждение рецепта в чате; current — выжимка текущего варианта;
    regenerate — «↻ Перегенерировать»: пожелания из обсуждения либо заметно другой вариант."""
    content = f"Блюдо: {name}. Порций: {servings}." + _dish_header(dish)
    if mention:
        content += f"\nЧто обещано о блюде в плане (выполни): {_clip(mention, 200)}"
    if request:
        content += f"\nИсходный запрос пользователя к плану (фон): {_clip(request, 300)}"
    content += _discussion_block(discussion)
    if current:
        content += f"\nТекущий вариант рецепта ({current})."
    if regenerate:
        content += _REGEN_RULE
    if change:
        content += f" Изменение рецепта (обязательно учти): {change}."
    content += as_hint(constraints_only=True)
    return [
        {"role": "system", "content": DISH_DETAIL_SYSTEM},
        {"role": "user", "content": content},
    ]


# --- Единый план готовки на всю неделю (batch-cooking) ---

# По ВСЕМ блюдам недели собираем ОДНУ оптимальную последовательность заготовки:
# группируем одинаковую подготовку (мойка/чистка/нарезка), общие компоненты (напр. фарш
# на несколько блюд) готовим один раз, длинные ПАССИВНЫЕ процессы запускаем раньше и
# параллелим с активной работой — чтобы минимизировать общее время.
COOKPLAN_SYSTEM = _SHARED_PREFIX + (
    "Тебе дан список ВСЕХ блюд недели с ингредиентами и шагами. Составь ОДИН общий "
    "оптимальный план заготовки на всю неделю (batch-cooking), а не отдельные рецепты. "
    "Принципы оптимизации: "
    "1) ОБЪЕДИНЯЙ одинаковую подготовку по всем блюдам — сначала помыть все овощи, потом "
    "почистить, потом нарезать; не повторяй одно и то же для каждого блюда. "
    "2) Общие компоненты (напр. фарш, соффрито, бульон, соус), которые нужны в нескольких "
    "блюдах, готовь ОДИН РАЗ и указывай, в какие блюда идёт. "
    "3) Длинные ПАССИВНЫЕ процессы (замачивание, маринование, варка бульона, тушение, "
    "запекание, остывание) запускай КАК МОЖНО РАНЬШЕ и ПАРАЛЛЕЛЬ с активной работой, чтобы "
    "общее время было минимальным. "
    "4) Группируй шаги по фазам (напр. «Подготовка», «Готовка», «Заморозка»). "
    "5) Каждый шаг — с порядковым номером и оценкой активного/пассивного времени в минутах "
    "(активное — когда ты занят руками; пассивное — когда процесс идёт сам). "
    "6) КОМПАКТНО: шаг — 1–2 коротких предложения; граммовки не перечисляй (они есть в "
    "рецептах), всего не больше 25 шагов — иначе ответ не влезет. "
    "Всё СТРОГО на русском. Верни СТРОГО JSON вида: "
    '{"steps": [{"order": число, "phase": "фаза", "text": "что сделать", '
    '"activeMin": число, "passiveMin": число, "dishes": ["название блюда"]}], '
    '"note": "короткий итог: примерное общее время и как распараллелено"}. '
    "dishes — названия блюд из списка, к которым относится шаг (пусто, если шаг общий). "
    "Порядок шагов (order) отражает реальную последовательность готовки."
)

# Схема плана готовки для Cloudflare-ветки (structured output).
COOKPLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "integer"},
                    "phase": {"type": "string"},
                    "text": {"type": "string"},
                    "activeMin": {"type": "integer"},
                    "passiveMin": {"type": "integer"},
                    "dishes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["order", "phase", "text", "activeMin", "passiveMin", "dishes"],
            },
        },
        "note": {"type": "string"},
    },
    "required": ["steps", "note"],
}


def build_cook_plan_messages(
    dishes: list[dict], *, discussion: str = "", regenerate: bool = False
) -> list[dict[str, str]]:
    """discussion — обсуждение плана готовки в чате; regenerate — «↻ Перегенерировать»."""
    blocks: list[str] = []
    for i, d in enumerate(dishes):
        ing = "; ".join(
            f"{x.get('name')} {x.get('qty')}{x.get('unit')}"
            for x in (d.get("ingredients") or [])
        )
        steps = "\n".join(
            f"  {j + 1}. {s}" for j, s in enumerate(d.get("steps") or [])
        )
        block = f"[{i + 1}] {d.get('name')} ({d.get('servings', 4)} порц.)"
        if ing:
            block += f"\nИнгредиенты: {ing}"
        if steps:
            block += f"\nШаги:\n{steps}"
        blocks.append(block)
    content = "Блюда недели:\n\n" + "\n\n".join(blocks)
    content += _discussion_block(discussion)
    if regenerate:
        content += (
            "\nПЕРЕСБОРКА плана готовки: есть пожелания из обсуждения — примени их; нет — "
            "пересобери заново, поищи порядок и параллели эффективнее прежних."
        )
    # план готовки: аллергии/ограничения — да (соусы, заправки), БЖУ — не при чём
    content += as_hint(constraints_only=True, macros=False)
    return [
        {"role": "system", "content": COOKPLAN_SYSTEM},
        {"role": "user", "content": content},
    ]


# --- Правка существующего плана через function calling ---

# Для ПРАВОК полный свод правил заморозки не нужен: шаг правки только выбирает функцию
# (add/remove/replace/edit/create), а сами рецепты/план генерит generate_plan/
# generate_dish_detail — там правила заморозки применяются. Короткое напоминание экономит
# ~1000 токенов на правку (на Claude/Gemini префикс не кэшируется, в отличие от DeepSeek).
_FREEZE_NOTE = (
    "Блюда — заготовки впрок (вакуум + заморозка); детали заморозки учитываются при "
    "генерации самих рецептов. "
)

EDIT_SYSTEM = COOK_PREAMBLE + _FREEZE_NOTE + (
    "Пользователь редактирует УЖЕ СОСТАВЛЕННЫЙ план на неделю (он показан ниже). "
    "Твоя задача — вызвать подходящие функции, чтобы применить его просьбу к ЭТОМУ плану. "
    "НЕ пересобирай меню целиком, если об этом явно не просят: для точечных правок используй "
    "add_dishes / remove_dish / replace_dish. create_plan вызывай ТОЛЬКО когда просят совсем "
    "другое меню (напр. «сделай вегетарианское», «сгенерируй заново»). "
    "Можно вызвать несколько функций за раз (напр. убрать одно и добавить другое). "
    "Если просят изменить ИНГРЕДИЕНТЫ или сам рецепт конкретного блюда (убрать/добавить/"
    "заменить продукт, сделать острее, меньше соли и т.п.) — это edit_dish, НЕ replace_dish "
    "(блюдо остаётся тем же, меняется только его рецепт). "
    "Если просьба не про изменение плана — не вызывай функции, коротко ответь текстом на русском. "
    "Названия блюд в remove_dish/replace_dish/edit_dish бери из списка ниже. "
    "Ниже может быть исходный запрос и недавние реплики диалога — опирайся на них, чтобы понять, "
    "какое блюдо имеется в виду (напр. «один суп» изначально значил конкретное блюдо); "
    "свежие указания важнее старых. "
    "Если из просьбы и контекста НЕ однозначно, КАКОЕ блюдо менять или убирать (подходят два и "
    "более, а конкретное не названо) — НЕ выбирай сам и не гадай: не вызывай функции и задай ОДИН "
    "короткий уточняющий вопрос (напр. «Убрать том ям или куриный суп?»). "
    "То же при замене/добавлении: если не задано КОНКРЕТНО, чем заменить или что добавить (только "
    "отрицание или размытая категория — «на не-суп», «что-нибудь простое») — не придумывай блюдо "
    "сам, а уточни, чего хочется (рыбное/мясное/овощное и т.п.). "
    "Исключение — когда явно просят «предложи сам / на твой вкус»: тогда подбирай без вопросов. "
    "Действуй сразу только когда блюдо и операция однозначны."
)

PLAN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_dishes",
            "description": "Добавить в план новые блюда под описание пользователя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Что за блюда добавить, напр. 'рыбное на пару', 'вегетарианское'",
                    },
                    "count": {"type": "integer", "description": "Сколько блюд добавить", "default": 1},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_dish",
            "description": "Убрать блюдо из плана по названию.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Название блюда из текущего плана"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_dish",
            "description": "Заменить блюдо из плана на новое под описание.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_name": {"type": "string", "description": "Название заменяемого блюда"},
                    "query": {"type": "string", "description": "Каким блюдом заменить"},
                },
                "required": ["old_name", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_dish",
            "description": (
                "Изменить рецепт конкретного блюда: убрать/добавить/заменить ингредиент, "
                "сделать острее, менее солёным и т.п. Блюдо остаётся тем же — перегенерируется "
                "его рецепт (ингредиенты и шаги)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Название блюда из текущего плана"},
                    "change": {
                        "type": "string",
                        "description": "Что изменить в рецепте, напр. 'убрать болгарский перец'",
                    },
                },
                "required": ["name", "change"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "Пересобрать меню целиком (только по явной просьбе о другом меню).",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Сколько блюд в новом плане"},
                    "note": {"type": "string", "description": "Пожелания к новому меню"},
                },
                "required": ["note"],
            },
        },
    },
]


def _plan_summary(title: str, dish_names: list[str]) -> str:
    lines = "\n".join(f"- {n}" for n in dish_names) or "(пусто)"
    return f"Текущий план «{title}», блюда:\n{lines}"


def build_edit_messages(
    title: str, dish_names: list[str], user_message: str, gender: str = "f", context: str = ""
) -> list[dict[str, str]]:
    content = _plan_summary(title, dish_names)
    if context:
        content += f"\n\n{context}"
    content += f"\n\nПросьба: {user_message.strip()}"
    content += _gender_hint(gender)
    return [
        {"role": "system", "content": EDIT_SYSTEM},
        {"role": "user", "content": content},
    ]


# Фолбэк без tools API (Cloudflare): та же логика через structured JSON.
EDIT_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Короткая реплика в чат, на русском"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "description": "add|remove|replace|edit|create"},
                    "query": {"type": "string"},
                    "name": {"type": "string"},
                    "change": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["op"],
            },
        },
    },
    "required": ["reply", "actions"],
}


def build_edit_action_messages(
    title: str, dish_names: list[str], user_message: str, context: str = ""
) -> list[dict[str, str]]:
    system = EDIT_SYSTEM + (
        " Верни СТРОГО JSON: {\"reply\": \"...\", \"actions\": [{\"op\": \"add|remove|replace|edit|create\", "
        "\"query\": \"...\", \"name\": \"...\", \"change\": \"...\", \"count\": число}]}. Для remove/replace/edit "
        "указывай name блюда; для edit — что поменять в поле change; для add/create — query/note в поле query; "
        "count — при add/create. Если нужно уточнить (см. правило про неоднозначность) — actions=[] и вопрос в reply."
    )
    content = _plan_summary(title, dish_names)
    if context:
        content += f"\n\n{context}"
    content += f"\n\nПросьба: {user_message.strip()}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def _gender_hint(gender: str) -> str:
    # Пол ассистента влияет только на прозу модели; кладём в USER-сообщение (не в system),
    # чтобы не менять кэшируемый общий префикс и чтобы смена пола применялась сразу.
    # Это род АССИСТЕНТА (в reply), а не пользователя — его род модель не угадывает.
    role = "МУЖСКОМ" if gender == "m" else "ЖЕНСКОМ"
    return f"\nО себе (ассистенте) пиши в {role} роде; это не про пользователя."


def _plan_count_hint(count: int) -> str:
    # count из селектора — это ДЕФОЛТ. Явное число в запросе пользователя важнее.
    return (
        f"\n\nКоличество блюд: если в запросе явно указано число (например «два ужина», "
        f"«5 блюд», «штук 6») — сделай ровно столько (1–12). Если не указано — сделай {count}."
    )


def _clip(text: str, n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# Лимит «недавно ели или отвергли» в промпте (сам список собирает services/history.py).
AVOID_CAP = 24


def _avoid_block(avoid_titles: list[str], cap: int = AVOID_CAP) -> str:
    if not avoid_titles:
        return ""
    return (
        "\nНедавно ели или отвергли — не повторяй и не делай близких вариаций (тот же основной "
        "продукт + форма/соус): " + ", ".join(avoid_titles[:cap])
    )


def _in_plan_block(in_plan: list[str] | None) -> str:
    # «Уже в этом плане» — отдельно от истории: это соседи по текущему меню.
    if not in_plan:
        return ""
    return "\nУже в этом плане (не дублируй): " + ", ".join(in_plan)


def _plan_user_content(
    user_message: str, avoid_titles: list[str], count: int, gender: str,
    in_plan: list[str] | None, variety: str, context: str, date_hint: str,
) -> str:
    # Всё динамическое — в USER-сообщении: system остаётся стабильным (кэш префикса DeepSeek).
    content = user_message.strip() + _plan_count_hint(count)
    if context:
        content += f"\n{context}"
    if date_hint:
        content += f"\n{date_hint}"
    content += _in_plan_block(in_plan) + _avoid_block(avoid_titles)
    if variety:
        content += f"\n{variety}"
    return content + _gender_hint(gender) + as_hint()


def build_ds_plan_messages(
    user_message: str, avoid_titles: list[str], count: int, gender: str = "f",
    *, in_plan: list[str] | None = None, variety: str = "", context: str = "",
    date_hint: str = "",
) -> list[dict[str, str]]:
    """План одним запросом. in_plan — блюда текущего плана (для add/create изнутри правки),
    variety — серверное зерно разнообразия, context — контекст беседы, date_hint — неделя/сезон."""
    content = _plan_user_content(
        user_message, avoid_titles, count, gender, in_plan, variety, context, date_hint
    )
    return [
        {"role": "system", "content": DEEPSEEK_PLAN_SYSTEM},
        {"role": "user", "content": content},
    ]


def build_names_messages(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f",
    *, in_plan: list[str] | None = None, variety: str = "", context: str = "",
    date_hint: str = "",
) -> list[dict[str, str]]:
    content = _plan_user_content(
        user_message, avoid_titles, count, gender, in_plan, variety, context, date_hint
    )
    return [
        {"role": "system", "content": NAMES_SYSTEM},
        {"role": "user", "content": content},
    ]


# --- Одно блюдо в уже составленный план (замена / добавление) ---

# Отдельный промпт на ОДНО блюдо: без подсказки про число блюд и без title — только короткая
# реплика и одно блюдо. Префикс общий с планом/деталью (кэшируется DeepSeek).
SINGLE_DISH_SYSTEM = _SHARED_PREFIX + (
    "Подбираешь ровно ОДНО блюдо в уже составленное меню на неделю (замена или добавление): "
    "реалистичное, дружелюбное к заморозке, без выдуманных названий. Оно должно отличаться от "
    "соседних блюд плана основным продуктом и способом готовки. Не предлагай то, что уже "
    "отвергнуто в этой беседе, и близкие к нему вариации. Учитывай ограничения пользователя. "
    "Верни СТРОГО JSON вида: "
    '{"reply": "одна короткая фраза", "dish": {"name": "короткое название", "emoji": "1 эмодзи", '
    '"servings": число, "prep_min": число, "cook_min": число, "tags": ["тег"], '
    '"garnish": "гарнир или пусто", '
    '"storage": {"vacuum": true, "freeze": true, "shelf_life_days": число 30-90}}}. '
    "Ровно одно блюдо, НЕ массив. Без ингредиентов, шагов и советов."
)

# Схема для Cloudflare (structured output) — то же одно блюдо.
SINGLE_DISH_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "dish": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "emoji": {"type": "string"},
                "servings": {"type": "integer"},
                "prep_min": {"type": "integer"},
                "cook_min": {"type": "integer"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "garnish": {"type": "string"},
            },
            "required": ["name", "emoji", "servings", "prep_min", "cook_min", "tags"],
        },
    },
    "required": ["reply", "dish"],
}


def _dish_line(d: dict) -> str:
    tags = [str(t) for t in (d.get("tags") or []) if t][:4]
    return f"{d.get('name')}" + (f" ({', '.join(tags)})" if tags else "")


def build_single_dish_messages(
    query: str,
    *,
    old_dish: dict | None = None,
    plan_dishes: list[dict] | None = None,
    rejected: list[str] | None = None,
    avoid_titles: list[str] | None = None,
    context: str = "",
    gender: str = "f",
) -> list[dict[str, str]]:
    """Замена (old_dish задан) или добавление одного блюда. plan_dishes — остальные блюда
    плана (с тегами), rejected — отвергнутое в ЭТОЙ беседе, avoid_titles — общая история."""
    if old_dish:
        head = f"Замени блюдо «{_dish_line(old_dish)}» на другое."
    else:
        head = "Добавь в план ещё одно блюдо."
    content = head
    if query.strip():
        content += f" Пожелание пользователя (важнее остального): {query.strip()}"
    if context:
        content += f"\n{context}"
    if plan_dishes:
        content += "\nОстальные блюда плана — не повторяй их основной продукт и способ: " + "; ".join(
            _dish_line(d) for d in plan_dishes
        )
    if rejected:
        content += "\nУже отвергнуто в этой беседе: " + ", ".join(rejected[:15])
    content += _avoid_block(avoid_titles or [], cap=15)
    content += _gender_hint(gender) + as_hint()
    return [
        {"role": "system", "content": SINGLE_DISH_SYSTEM},
        {"role": "user", "content": content},
    ]


def build_dish_messages(name: str, user_message: str) -> list[dict[str, str]]:
    content = (
        f"Блюдо: {name}. Общий запрос пользователя (учти порции/ограничения): {user_message}"
        + as_hint(constraints_only=True)
    )
    return [
        {"role": "system", "content": DISH_SYSTEM},
        {"role": "user", "content": content},
    ]


# --- Обсуждение цели в чате («💬 Обсудить в чате»): рецепт / план готовки / список покупок ---

# Общий префикс тот же (кэшируется DeepSeek); вся динамика (содержимое цели, реплики) — в
# user/assistant-сообщениях. Два стабильных варианта system: для tools (DeepSeek) и для JSON.
DISCUSS_SYSTEM = _SHARED_PREFIX + (
    "Сейчас ты обсуждаешь с пользователем ОДНУ цель из его плана заготовок на неделю: рецепт "
    "блюда, общий план готовки или список покупок. Цель и её полное содержание — в первом "
    "сообщении. Отвечай кратко и по делу, в markdown (короткие абзацы, списки), на «ты», "
    "строго на русском. НИЧЕГО не меняй, если об этом явно не просят: вопросы, советы, "
    "пояснения, варианты «на подумать» — просто ответ текстом. "
    "Если пользователь ЯВНО просит изменить рецепт (убрать/добавить/заменить продукт, острее, "
    "меньше соли, приготовить иначе) — это ПРАВКА РЕЦЕПТА: кратко опиши изменение. "
    "Если явно просит заменить блюдо целиком другим — это ЗАМЕНА БЛЮДА: сам не заменяй, "
    "передай, чем заменить. Для плана готовки и списка покупок явная просьба что-то "
    "поменять — это ПЕРЕСБОРКА цели: кратко опиши, что поменять. "
    "Не утверждай, что уже что-то изменил, — изменение применяет система после твоего ответа. "
)

DISCUSS_TOOLS_RULE = (
    "Правку рецепта делай функцией update_recipe, замену блюда — replace_dish, пересборку "
    "плана готовки или списка покупок — regenerate. Без явной просьбы функции не вызывай. "
    "Текст ответа пользователю — обычным сообщением."
)

DISCUSS_JSON_RULE = (
    'Верни СТРОГО JSON: {"reply": "ответ пользователю в markdown", "action": {"op": '
    '"none|edit|replace|regenerate", "change": "что изменить", "query": "чем заменить"}}. '
    "op: none — менять ничего не просили; edit — правка рецепта (change); replace — замена "
    "блюда (query); regenerate — пересборка плана готовки/списка покупок (change)."
)

DISCUSS_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Ответ пользователю в markdown, на русском"},
        "action": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "description": "none|edit|replace|regenerate"},
                "change": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["op"],
        },
    },
    "required": ["reply", "action"],
}


def _fn(name: str, description: str, arg: str, arg_desc: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {arg: {"type": "string", "description": arg_desc}},
                "required": [arg],
            },
        },
    }


# Небольшой набор функций на цель: рецепт — правка/замена, готовка и покупки — пересборка.
DISCUSS_TOOLS = {
    "recipe": [
        _fn(
            "update_recipe",
            "Изменить рецепт обсуждаемого блюда по явной просьбе (блюдо то же, меняется рецепт).",
            "change", "Что изменить, напр. 'без болгарского перца, острее'",
        ),
        _fn(
            "replace_dish",
            "Пользователь явно хочет заменить обсуждаемое блюдо целиком другим.",
            "query", "Чем заменить, напр. 'рыбное на пару' (может быть пусто)",
        ),
    ],
    "cooking": [
        _fn(
            "regenerate",
            "Пересобрать план готовки с учётом явной просьбы пользователя.",
            "change", "Что поменять в плане готовки",
        ),
    ],
    "shopping": [
        _fn(
            "regenerate",
            "Пересобрать список покупок с учётом явной просьбы пользователя.",
            "change", "Что поменять в списке покупок",
        ),
    ],
}

_DISCUSS_TITLES = {"recipe": "рецепт", "cooking": "план готовки", "shopping": "список покупок"}


def discuss_recipe_context(dish: dict, others: list[str], request: str = "") -> str:
    """Полный контекст рецепта: шапка из плана + ингредиенты/шаги/советы/заметка."""
    parts = [
        f"Обсуждаем рецепт: «{dish.get('name')}» ({dish.get('servings', 4)} порц.)."
        + _dish_header(dish)
    ]
    ings = dish.get("ingredients") or []
    if ings:
        parts.append("Ингредиенты: " + "; ".join(
            f"{i.get('name')} {i.get('qty')} {i.get('unit')}" for i in ings
        ))
    steps = dish.get("steps") or []
    if steps:
        parts.append("Шаги:\n" + "\n".join(f"{j + 1}. {s}" for j, s in enumerate(steps)))
    tips = dish.get("tips") or []
    if tips:
        parts.append("Советы: " + " ".join(str(t) for t in tips))
    note = (dish.get("storage") or {}).get("note")
    if note:
        parts.append(f"Хранение/разогрев: {note}")
    if others:
        parts.append("Другие блюда плана: " + ", ".join(others))
    if request:
        parts.append(f"Исходный запрос пользователя к плану: {_clip(request, 300)}")
    return "\n".join(parts)


def discuss_cooking_context(cooking: dict, dishes: list[str], request: str = "") -> str:
    """Контекст плана готовки: шаги по фазам с таймингами + итог."""
    parts = ["Обсуждаем общий план готовки на неделю. Блюда: " + (", ".join(dishes) or "—")]
    steps = sorted(cooking.get("steps") or [], key=lambda s: s.get("order") or 0)
    if steps:
        parts.append("Шаги:\n" + "\n".join(
            f"{s.get('order')}. [{s.get('phase') or 'Готовка'}] {s.get('text')} "
            f"(активно {s.get('active_min', 0)} мин, пассивно {s.get('passive_min', 0)} мин)"
            for s in steps
        ))
    else:
        parts.append("План готовки ещё не собран.")
    if cooking.get("note"):
        parts.append(f"Итог: {cooking['note']}")
    if request:
        parts.append(f"Исходный запрос пользователя к плану: {_clip(request, 300)}")
    return "\n".join(parts)


def discuss_shopping_context(items: list[dict], dishes: list[str], request: str = "") -> str:
    """Контекст списка покупок: позиции с количествами + блюда плана."""
    parts = ["Обсуждаем список покупок на неделю. Блюда: " + (", ".join(dishes) or "—")]
    if items:
        parts.append("Позиции:\n" + "\n".join(
            f"- {i.get('name')} — {i.get('qty')} {i.get('unit')} ({i.get('category')})"
            for i in items
        ))
    if request:
        parts.append(f"Исходный запрос пользователя к плану: {_clip(request, 300)}")
    return "\n".join(parts)


def build_discuss_messages(
    target: str,
    context: str,
    turns: list[dict[str, str]],
    question: str,
    gender: str = "f",
    *,
    tools: bool = False,
) -> list[dict[str, str]]:
    """Мульти-тёрн обсуждения: system (стабильный) → контекст цели + прошлые реплики →
    текущий вопрос. Контекст идёт первым user-сообщением (стабильный префикс для кэша), к нему
    приклеиваем первую реплику, если она пользовательская (роли должны чередоваться)."""
    system = DISCUSS_SYSTEM + (DISCUSS_TOOLS_RULE if tools else DISCUSS_JSON_RULE)
    head = f"{context}\n\n(Обсуждаем {_DISCUSS_TITLES.get(target, target)}.)"
    if target != "shopping":
        # аллергии/ограничения — в советах по рецепту и готовке тоже (в покупках не нужны);
        # БЖУ — только к рецепту. Кладём в контекст (первое user-сообщение), system стабилен.
        head += as_hint(constraints_only=True, macros=target == "recipe")
    msgs: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": head},
    ]
    current = {"role": "user", "content": question.strip() + _gender_hint(gender)}
    for t in [*turns, current]:
        if msgs[-1]["role"] == t["role"]:
            msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"] + "\n\n" + t["content"]}
        else:
            msgs.append(dict(t))
    return msgs


# --- Сводка беседы (services/summary.py) ---
# Фоновый дешёвый вызов после реплик пользователя (дебаунс). Результат — ОДНА сводка на беседу,
# она подмешивается в контекст всех генераций чата вместе с первым сообщением пользователя.

SUMMARY_MAX_CHARS = 700

SUMMARY_SYSTEM = (
    "Ты ведёшь краткую память беседы пользователя с кулинарным ассистентом, который составляет "
    "меню на неделю. По прошлой сводке (если есть) и новым репликам напиши ОБНОВЛЁННУЮ сводку "
    "ВСЕЙ беседы: чего пользователь хочет от плана, явные условия и пожелания, что уже сделано "
    "(какие блюда добавлены, убраны или заменены, что пользователь отверг или одобрил), "
    "открытые вопросы. Только факты из реплик — ничего не выдумывай и не советуй. "
    f"Кратко: 3–8 пунктов, каждый с «- », всего не больше {SUMMARY_MAX_CHARS} символов, на русском. "
    'Верни СТРОГО JSON: {"summary": "- ...\\n- ..."}'
)

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def build_summary_messages(previous: str, lines: list[str]) -> list[dict[str, str]]:
    """previous — прошлая сводка (или пусто), lines — новые реплики «Пользователь: …»/«Ассистент: …»."""
    content = ""
    if previous.strip():
        content += "Прошлая сводка:\n" + previous.strip() + "\n\n"
    content += "Новые реплики:\n" + "\n".join(lines)
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": content},
    ]


def chat_memory_block(first_message: str, summary: str) -> str:
    """Память беседы для генераций в чате: первое сообщение пользователя (всегда, если есть)
    + последняя сводка беседы (если уже есть). Пусто — нечего добавить."""
    parts: list[str] = []
    if first_message.strip():
        parts.append(f"Первое сообщение пользователя (с чего начался чат): {_clip(first_message, 400)}")
    if summary.strip():
        parts.append(
            "Сводка беседы до этого момента (сжато, последние реплики могут в неё не войти):\n"
            + summary.strip()[: SUMMARY_MAX_CHARS + 200]
        )
    return "\n".join(parts)
