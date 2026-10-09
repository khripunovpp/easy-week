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


def running(key: Hashable) -> bool:
    """Идёт ли уже работа с этим ключом (для лога «ждём соседа»)."""
    return key in _inflight


async def single_flight(key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
    """Результат factory() для key: своя задача или уже идущая с тем же ключом."""
    current = _inflight.get(key)
    if current is not None:
        return await asyncio.shield(current)
    fut = asyncio.ensure_future(factory())
    _inflight[key] = fut
    try:
        return await fut
    finally:
        if _inflight.get(key) is fut:
            del _inflight[key]
