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
    "Опиши блюдо для заготовки впрок (вакуум + заморозка). "
    "Единицы метрические. Тайминги реалистичные. "
    "storage: vacuum=true, freeze=true, реальный shelf_life_days (обычно 30–90), "
    "короткая note о разморозке. ingredients — с количествами. "
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

# Ленивая догенерация шагов и советов для одного блюда.
DETAILS_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps", "tips"],
}


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


def build_details_messages(name: str, ingredients: list[dict]) -> list[dict[str, str]]:
    ing = ", ".join(f"{i.get('name')} {i.get('qty')}{i.get('unit')}" for i in ingredients)
    return [
        {
            "role": "system",
            "content": (
                "Дай пошаговый рецепт блюда для заготовки впрок (вакуум + заморозка). "
                "steps — 3–6 кратких шагов на русском. tips — 1–2 совета про приготовление "
                "и заморозку/разморозку. Компактно, без воды."
            ),
        },
        {"role": "user", "content": f"Блюдо: {name}. Ингредиенты: {ing}."},
    ]
