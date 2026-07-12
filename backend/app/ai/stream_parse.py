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


class ArrayStreamParser:
    """Из растущего буфера отдаёт по одному завершённые объекты массива `key`."""

    def __init__(self, key: str) -> None:
        self.buf = ""
        self._emitted = 0
        self._array_re = re.compile(rf'"{re.escape(key)}"\s*:\s*\[')

    def feed(self, chunk: str) -> None:
        self.buf += chunk

    def new_objects(self) -> list[dict]:
        objs = _complete_objects(self.buf, self._array_re)
        parsed: list[dict] = []
        for raw in objs[self._emitted :]:
            try:
                parsed.append(json.loads(raw))
            except json.JSONDecodeError:
                break  # объект ещё дописывается — подождём следующего feed
        self._emitted += len(parsed)
        return parsed


class PlanStreamParser(ArrayStreamParser):
    """План: meta (reply/title, идут раньше) + блюда из массива dishes по одному."""

    def __init__(self) -> None:
        super().__init__("dishes")

    def meta(self) -> dict[str, str] | None:
        r = _REPLY_RE.search(self.buf)
        t = _TITLE_RE.search(self.buf)
        if not (r and t):
            return None
        return {"reply": _unescape(r.group(1)), "title": _unescape(t.group(1))}

    def new_dishes(self) -> list[dict]:
        return self.new_objects()


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s


def _complete_objects(buf: str, array_re: re.Pattern[str]) -> list[str]:
    """Строки полностью закрытых {..}-объектов внутри указанного массива."""
    m = array_re.search(buf)
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
