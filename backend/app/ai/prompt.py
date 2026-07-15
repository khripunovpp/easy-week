from .prefs import as_hint

NAMES_SYSTEM = (
    "Ты — помощник по меню на неделю для заготовок впрок (вакуум + заморозка). "
    "Подбери блюда под запрос, которые удобно заморозить порциями. "
    "ВАЖНО — РАЗНООБРАЗИЕ: блюда должны отличаться по основному продукту, "
    "не делай всё из одного (например, не все с курицей). Чередуй белки и основы: "
    "птица, говядина/телятина, рыба/морепродукты, бобовые/тофу, овощи. "
    "Разные способы готовки (тушение, запекание, суп, котлеты/тефтели). "
    "Учитывай ограничения (аллергии, без свинины и т.п.). "
    "Не повторяй недавно принятые блюда. Ответ компактный, на русском. "
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
    "(например лосось/треска — 'Рыба'). Название — короткое, с маленькой буквы."
)


def build_shop_normalize_messages(items: list[dict]) -> list[dict[str, str]]:
    lines = [
        f"{it.get('name')} — {it.get('qty')} {it.get('unit')} — {it.get('category')}"
        for it in items
    ]
    return [
        {"role": "system", "content": SHOP_SYSTEM},
        {"role": "user", "content": "Список:\n" + "\n".join(lines)},
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
    "Вводить ПОСЛЕ разморозки/при разогреве (расслаиваются): сливки, сметану, молоко, йогурт, "
    "мягкий и плавленый сыр; соусы-загустители на муке или крахмале (бешамель, заварные — "
    "размешивай при разогреве). Твёрдый сыр после разморозки крошится — только в готовку/запекание. "
    "Морозить СЫРЫМИ и готовить из заморозки (лучше текстура): котлеты, тефтели, фрикадельки "
    "(формованные; готовыми — только если цель «достал и разогрел», тогда укажи это в note); "
    "изделия в панировке и кляре (шницели, наггетсы — жарить прямо из морозилки, иначе теряют "
    "хруст). Грибы перед заморозкой обжарь (сырые водянистые). Уже размороженные мясо и рыбу "
    "повторно не замораживай. "
    "Хорошо морозятся готовыми: тушёное мясо, супы без сливок и картофеля, бульоны, томатные и "
    "масляные соусы, рагу, запеканки, голубцы, фаршированные перцы. Гарниры (паста, картофель, "
    "рис) лучше свежими — морозим белок, основу и соус. "
    "Проблемные компоненты можно оставлять в блюде, но в шагах и note явно писать, что они "
    "добавляются/готовятся после разморозки, при подаче или разогреве. "
)

_SHARED_PREFIX = COOK_PREAMBLE + FREEZE_RULES

# План отдаёт только «шапку» блюда (то, что видно на карточке). Ингредиенты, шаги, советы
# и заметку о хранении генерим лениво при открытии блюда (build_dish_detail_messages).
DEEPSEEK_PLAN_SYSTEM = _SHARED_PREFIX + (
    "Составляешь меню на неделю: подбери РАЗНЫЕ реалистичные блюда (разные белки: птица, "
    "говядина, рыба, бобовые/тофу, овощи), дружелюбные к заморозке, без выдуманных названий. "
    "Учитывай ограничения пользователя (без свинины и т.п.). "
    "Верни СТРОГО JSON вида: "
    '{"reply": "короткая реплика", "title": "название плана 2-3 слова", '
    '"dishes": [{"name": "короткое название", "emoji": "1 эмодзи", "servings": число, '
    '"prep_min": число, "cook_min": число, "tags": ["тег"], '
    '"storage": {"vacuum": true, "freeze": true, "shelf_life_days": число 30-90}}]}. '
    "НЕ добавляй ингредиенты, шаги и советы — только эти поля. "
    "Количества/тайминги реалистичны на порции."
)


# Ленивая ПОЛНАЯ деталь одного блюда: ингредиенты + развёрнутые шаги + советы + заметка хранения.
DISH_DETAIL_SYSTEM = _SHARED_PREFIX + (
    "Дай ПОЛНЫЙ рецепт блюда. Верни СТРОГО JSON вида: "
    '{"ingredients": [{"name": "продукт", "qty": число, "unit": "г|мл|шт", "category": "категория"}], '
    '"steps": ["шаг 1", "шаг 2", "..."], "tips": ["совет"], "note": "как разморозить/разогреть"}. '
    "ЕДИНИЦЫ ингредиентов: только 'г' (вес) или 'мл' (жидкости); 'шт' — лишь для штучного "
    "(яйца, лавровый лист, ванильный стручок). Никаких ложек/щепоток/зубчиков — переводи в граммы "
    "(ч.л.≈5 г, ст.л.≈15 г, зубчик≈5 г, стакан≈200 г). Количества реалистичны на указанные порции. "
    "steps — 6–9 РАЗВЁРНУТЫХ шагов: как нарезать, температуры, тайминги, до какого состояния. "
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


def build_dish_detail_messages(
    name: str, servings: int, change: str = ""
) -> list[dict[str, str]]:
    content = f"Блюдо: {name}. Порций: {servings}."
    if change:
        content += f" Изменение рецепта (обязательно учти): {change}."
    content += as_hint()
    return [
        {"role": "system", "content": DISH_DETAIL_SYSTEM},
        {"role": "user", "content": content},
    ]


# --- Правка существующего плана через function calling ---

EDIT_SYSTEM = _SHARED_PREFIX + (
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
    "Названия блюд в remove_dish/replace_dish/edit_dish бери из списка ниже."
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
    title: str, dish_names: list[str], user_message: str, gender: str = "f"
) -> list[dict[str, str]]:
    content = f"{_plan_summary(title, dish_names)}\n\nПросьба: {user_message.strip()}"
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
    title: str, dish_names: list[str], user_message: str
) -> list[dict[str, str]]:
    system = EDIT_SYSTEM + (
        " Верни СТРОГО JSON: {\"reply\": \"...\", \"actions\": [{\"op\": \"add|remove|replace|edit|create\", "
        "\"query\": \"...\", \"name\": \"...\", \"change\": \"...\", \"count\": число}]}. Для remove/replace/edit "
        "указывай name блюда; для edit — что поменять в поле change; для add/create — query/note в поле query; "
        "count — при add/create."
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"{_plan_summary(title, dish_names)}\n\nПросьба: {user_message.strip()}",
        },
    ]


def _gender_hint(gender: str) -> str:
    # Пол ассистента влияет только на прозу модели; кладём в USER-сообщение (не в system),
    # чтобы не менять кэшируемый общий префикс и чтобы смена пола применялась сразу.
    role = "МУЖСКОМ" if gender == "m" else "ЖЕНСКОМ"
    return f"\nО себе и своих действиях пиши в {role} роде."


def _plan_count_hint(count: int) -> str:
    # count из селектора — это ДЕФОЛТ. Явное число в запросе пользователя важнее.
    return (
        f"\n\nКоличество блюд: если в запросе явно указано число (например «два ужина», "
        f"«5 блюд», «штук 6») — сделай ровно столько (1–12). Если не указано — сделай {count}."
    )


def build_ds_plan_messages(
    user_message: str, avoid_titles: list[str], count: int, gender: str = "f"
) -> list[dict[str, str]]:
    content = user_message.strip() + _plan_count_hint(count)
    if avoid_titles:
        content += "\nНедавно принятые блюда (не повторяй): " + ", ".join(avoid_titles[:12])
    content += _gender_hint(gender) + as_hint()
    return [
        {"role": "system", "content": DEEPSEEK_PLAN_SYSTEM},
        {"role": "user", "content": content},
    ]


def build_names_messages(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f"
) -> list[dict[str, str]]:
    content = user_message.strip() + _plan_count_hint(count)
    if avoid_titles:
        content += "\nНедавно принятые блюда (не повторяй): " + ", ".join(avoid_titles[:12])
    content += _gender_hint(gender) + as_hint()
    return [
        {"role": "system", "content": NAMES_SYSTEM},
        {"role": "user", "content": content},
    ]


def build_dish_messages(name: str, user_message: str) -> list[dict[str, str]]:
    content = (
        f"Блюдо: {name}. Общий запрос пользователя (учти порции/ограничения): {user_message}"
        + as_hint()
    )
    return [
        {"role": "system", "content": DISH_SYSTEM},
        {"role": "user", "content": content},
    ]
