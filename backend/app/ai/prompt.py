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


# План отдаёт только «шапку» блюда (то, что видно на карточке). Ингредиенты, шаги, советы
# и заметку о хранении генерим лениво при открытии блюда (build_dish_detail_messages).
DEEPSEEK_PLAN_SYSTEM = (
    "Ты — шеф-повар, составляешь меню на неделю для заготовок впрок (вакуум + заморозка). "
    "Подбери РАЗНЫЕ реалистичные блюда (разные белки: птица, говядина, рыба, бобовые/тофу, овощи), "
    "без выдуманных названий. Учитывай ограничения пользователя (без свинины и т.п.). "
    "Верни СТРОГО JSON вида: "
    '{"reply": "короткая реплика", "title": "название плана 2-3 слова", '
    '"dishes": [{"name": "короткое название", "emoji": "1 эмодзи", "servings": число, '
    '"prep_min": число, "cook_min": число, "tags": ["тег"], '
    '"storage": {"vacuum": true, "freeze": true, "shelf_life_days": число 30-90}}]}. '
    "НЕ добавляй ингредиенты, шаги и советы — только эти поля. "
    "Количества/тайминги реалистичны на порции. "
    "Всё СТРОГО на русском — без латиницы и иероглифов (кроме эмодзи)."
)


# Ленивая ПОЛНАЯ деталь одного блюда: ингредиенты + развёрнутые шаги + советы + заметка хранения.
DISH_DETAIL_SYSTEM = (
    "Дай ПОЛНЫЙ рецепт блюда для заготовки впрок (вакуум + заморозка). "
    "Верни СТРОГО JSON вида: "
    '{"ingredients": [{"name": "продукт", "qty": число, "unit": "г|мл|шт", "category": "категория"}], '
    '"steps": ["шаг 1", "шаг 2", "..."], "tips": ["совет"], "note": "как разморозить/разогреть"}. '
    "ЕДИНИЦЫ ингредиентов: только 'г' (вес) или 'мл' (жидкости); 'шт' — лишь для штучного "
    "(яйца, лавровый лист, ванильный стручок). Никаких ложек/щепоток/зубчиков — переводи в граммы "
    "(ч.л.≈5 г, ст.л.≈15 г, зубчик≈5 г, стакан≈200 г). Количества реалистичны на указанные порции. "
    "steps — 6–9 РАЗВЁРНУТЫХ шагов: как нарезать, температуры, тайминги, до какого состояния. "
    "tips — 1–2 совета (готовка, порционирование, вакуум, заморозка/разморозка). "
    "category из: 'Мясо и птица','Рыба','Овощи','Молочное','Бакалея','Специи','Прочее'. "
    "Всё СТРОГО на русском — без латиницы и иероглифов."
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


def build_dish_detail_messages(name: str, servings: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": DISH_DETAIL_SYSTEM},
        {"role": "user", "content": f"Блюдо: {name}. Порций: {servings}."},
    ]


def build_ds_plan_messages(
    user_message: str, avoid_titles: list[str], count: int
) -> list[dict[str, str]]:
    content = f"{user_message.strip()}\n\nСоставь РОВНО {count} блюд(а)."
    if avoid_titles:
        content += "\nНедавно принятые блюда (не повторяй): " + ", ".join(avoid_titles[:12])
    return [
        {"role": "system", "content": DEEPSEEK_PLAN_SYSTEM},
        {"role": "user", "content": content},
    ]


def build_names_messages(
    user_message: str, avoid_titles: list[str], count: int = 5
) -> list[dict[str, str]]:
    content = f"{user_message.strip()}\n\nСоставь РОВНО {count} блюд(а)."
    if avoid_titles:
        content += "\nНедавно принятые блюда (не повторяй): " + ", ".join(avoid_titles[:12])
    return [
        {"role": "system", "content": NAMES_SYSTEM},
        {"role": "user", "content": content},
    ]


def build_dish_messages(name: str, user_message: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": DISH_SYSTEM},
        {
            "role": "user",
            "content": f"Блюдо: {name}. Общий запрос пользователя (учти порции/ограничения): {user_message}",
        },
    ]
