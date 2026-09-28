"""Причины 👎 по типам ответов — единый каталог для фронта (выпадашка) и метрик.

Ключи стабильные (идут в БД и в лейбл Prometheus `reason`), подписи можно править.
Набор собран по реальным жалобам из AI-логов (правки в чате, «перегенерируй потому что…»).
«Другое» (`other`) есть у всех — к нему свободный текст в `note`."""

OTHER = "other"

REASONS: dict[str, list[tuple[str, str]]] = {
    "recipe": [
        ("wrong", "Неправильный или невкусный рецепт"),
        ("not_authentic", "Не по-настоящему: чужие специи и ноты"),
        ("too_complex", "Слишком сложно, много шагов"),
        ("too_long", "Слишком долго готовить"),
        ("hard_ingredients", "Продукты сложно достать"),
        ("disliked_ingredient", "Есть то, что я не ем"),
        ("amounts", "Неверные количества или порции"),
        ("not_freezable", "Плохо переживёт заморозку"),
        ("ignored_request", "Не учёл мою просьбу"),
    ],
    "plan": [
        ("ignored_request", "Не учёл просьбу: не те блюда или их число"),
        ("repetitive", "Однообразно: повторяются продукты"),
        ("disliked_ingredient", "Есть то, что я не ем"),
        ("not_authentic", "Странные сочетания, всё в одном стиле"),
        ("too_complex", "Слишком сложные блюда"),
        ("boring", "Банально, хочется интереснее"),
        ("hard_ingredients", "Продукты сложно достать"),
        ("not_freezable", "Плохо подходит для заморозки"),
    ],
    "cooking": [
        ("wrong_order", "Неудобный порядок шагов"),
        ("unrealistic_timing", "Нереальные тайминги"),
        ("too_parallel", "Слишком много дел одновременно"),
        ("missing", "Пропущены шаги или блюда"),
        ("mismatch", "Расходится с рецептами"),
        ("too_long", "Слишком долго в сумме"),
    ],
    "shopping": [
        ("wrong_amounts", "Неверные количества или единицы"),
        ("duplicates", "Дубли: одно и то же разными строками"),
        ("missing", "Не хватает продуктов"),
        ("extra", "Лишние продукты"),
        ("bad_names", "Непонятные названия"),
        ("wrong_category", "Не в том отделе"),
    ],
    "message": [
        ("misunderstood", "Не понял, что я прошу"),
        ("wrong_change", "Изменил или удалил не то"),
        ("unwanted_change", "Поменял план, а я просто спрашивал"),
        ("ignored_request", "Не сделал, что просил"),
        ("wrong_facts", "Ошибся в фактах"),
        ("too_long", "Слишком длинно"),
    ],
}


def catalog() -> dict[str, list[dict[str, str]]]:
    """Для фронта: {target_type: [{key, label}, …, other]}."""
    return {
        t: [{"key": k, "label": lbl} for k, lbl in items] + [{"key": OTHER, "label": "Другое"}]
        for t, items in REASONS.items()
    }


def clean(target_type: str, reasons: list[str]) -> list[str]:
    """Оставить только известные ключи типа (порядок каталога, без дублей) — лейблы метрик
    не должны разрастаться от произвольных строк."""
    allowed = [k for k, _ in REASONS.get(target_type, [])] + [OTHER]
    got = set(reasons or [])
    return [k for k in allowed if k in got]
