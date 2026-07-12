"""Инкрементальный разбор потокового JSON плана.

DeepSeek стримит один JSON-объект вида
{"reply": "...", "title": "...", "dishes": [ {...}, {...} ]}.
Пока он печатается, достаём: meta (reply+title, они идут до dishes) и по одному
завершённые объекты из массива dishes — чтобы отдавать их во фронт по мере готовности.
"""

import json
import re

_STR = r'"((?:[^"\\]|\\.)*)"'
_REPLY_RE = re.compile(r'"reply"\s*:\s*' + _STR)
_TITLE_RE = re.compile(r'"title"\s*:\s*' + _STR)
_DISHES_RE = re.compile(r'"dishes"\s*:\s*\[')


class PlanStreamParser:
    """Держит растущий буфер и отдаёт meta один раз + новые блюда по мере закрытия."""

    def __init__(self) -> None:
        self.buf = ""
        self._emitted = 0  # сколько объектов dishes уже отдали

    def feed(self, chunk: str) -> None:
        self.buf += chunk

    def meta(self) -> dict[str, str] | None:
        """(reply, title) — как только оба доступны (они идут раньше dishes)."""
        r = _REPLY_RE.search(self.buf)
        t = _TITLE_RE.search(self.buf)
        if not (r and t):
            return None
        return {"reply": _unescape(r.group(1)), "title": _unescape(t.group(1))}

    def new_dishes(self) -> list[dict]:
        """Полностью закрытые объекты массива dishes, ещё не отданные."""
        objs = _complete_objects(self.buf)
        fresh = objs[self._emitted :]
        parsed: list[dict] = []
        for raw in fresh:
            try:
                parsed.append(json.loads(raw))
            except json.JSONDecodeError:
                break  # объект ещё дописывается — подождём следующего feed
        self._emitted += len(parsed)
        return parsed


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s


def _complete_objects(buf: str) -> list[str]:
    """Строки полностью закрытых {..}-объектов внутри массива dishes."""
    m = _DISHES_RE.search(buf)
    if not m:
        return []
    i = m.end()
    objs: list[str] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    while i < len(buf):
        c = buf[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objs.append(buf[start : i + 1])
                start = None
        elif c == "]" and depth == 0:
            break
        i += 1
    return objs
