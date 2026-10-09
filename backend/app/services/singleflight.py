"""Склейка одинаковой параллельной работы (single-flight) внутри процесса.

Пока идёт работа с ключом key, такие же вызовы ждут ТУ ЖЕ задачу — без повторного вызова
модели и двойной записи в БД. Процесс один (uvicorn без --workers), поэтому словаря в памяти
достаточно; очереди/брокер не нужны. Где: рецепт блюда / план готовки / ↻ покупок
(routers/plans.py) и догенерация рецептов для покупок/PDF/готовки (services/regenerate).

- Ошибку задачи получают все, кто её ждал; запись о задаче снимается в finally — сбой не
  «залипает», следующий вызов начинает заново.
- Ведущий (первый вызов) ждёт задачу напрямую: его отмена отменяет и работу — она идёт в его
  сессии БД. Остальные ждут через shield: их отмена (клиент ушёл) работу ведущего не трогает.
"""

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar

T = TypeVar("T")

_inflight: dict[Hashable, asyncio.Future] = {}
# Порядок регистрации работ — join ждёт только начатых раньше (иначе две работы, ждущие друг
# друга, повисли бы навсегда).
_order: dict[Hashable, int] = {}
_counter = 0


def running(key: Hashable) -> bool:
    """Идёт ли уже работа с этим ключом (для лога «ждём соседа»)."""
    return key in _inflight


async def single_flight(key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
    """Результат factory() для key: своя задача или уже идущая с тем же ключом."""
    current = _inflight.get(key)
    if current is not None:
        return await asyncio.shield(current)
    global _counter
    fut = asyncio.ensure_future(factory())
    _inflight[key] = fut
    _counter += 1
    _order[key] = _counter
    try:
        return await fut
    finally:
        if _inflight.get(key) is fut:
            del _inflight[key]
            _order.pop(key, None)


async def join(match: Callable[[Hashable], bool], own: Hashable | None = None) -> bool:
    """Дождаться идущих работ, чей ключ подходит под match, не начиная своей (разные
    эндпоинты, одна цель: догенерация рецепта для покупок и открытие того же рецепта).
    own — ключ своей работы (вызов изнутри single_flight): ждём только начатых РАНЬШЕ неё —
    две работы, ждущие друг друга, не повиснут. Ошибку чужой работы не пробрасываем — ждущий
    сам решит, делать ли своё. True — ждали."""
    limit = _order.get(own, float("inf")) if own is not None else float("inf")
    running = [
        fut for key, fut in list(_inflight.items())
        if key != own and match(key) and _order.get(key, 0) < limit
    ]
    for fut in running:
        try:
            await asyncio.shield(fut)
        except Exception:  # noqa: BLE001 — сбой соседа: ждущий попробует сам
            pass
    return bool(running)
