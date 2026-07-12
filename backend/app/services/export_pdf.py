"""Генерация PDF плана (fpdf2). Кириллица — через системный DejaVu Sans."""

from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from ..schemas import ShoppingGroup, WeekPlan


def _mc(pdf: FPDF, h: float, text: str, w: float = 0) -> None:
    """multi_cell с возвратом курсора к левому полю (иначе fpdf2 падает на след. строке)."""
    pdf.multi_cell(w, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
]
_BOLD_PATHS = [p.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf") for p in _REG_PATHS]

INK = (34, 29, 26)
MUTE = (141, 133, 127)
ACCENT = (236, 106, 71)
FROST = (63, 107, 136)


def _find(paths: list[str]) -> str | None:
    return next((p for p in paths if Path(p).exists()), None)


def _h2(pdf: FPDF, fam: str, text: str) -> None:
    pdf.ln(3)
    pdf.set_font(fam, "B", 15)
    pdf.set_text_color(*ACCENT)
    _mc(pdf, 8, text)
    pdf.set_draw_color(245, 210, 200)
    pdf.set_line_width(0.6)
    y = pdf.get_y() + 1
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(3)


def build_plan_pdf(
    plan: WeekPlan,
    shopping: list[ShoppingGroup],
    *,
    recipes: bool = True,
    shop: bool = True,
) -> bytes:
    reg = _find(_REG_PATHS)
    bold = _find(_BOLD_PATHS)

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(16, 16, 16)
    pdf.add_page()

    if reg:
        pdf.add_font("DejaVu", "", reg)
        pdf.add_font("DejaVu", "B", bold or reg)
        fam = "DejaVu"
    else:  # шрифта нет — не падаем (кириллица не отрисуется, но документ будет)
        fam = "Helvetica"

    # Заголовок
    pdf.set_font(fam, "B", 20)
    pdf.set_text_color(*INK)
    _mc(pdf, 9, plan.title)
    pdf.set_font(fam, "", 10)
    pdf.set_text_color(*MUTE)
    _mc(pdf, 6, f"Меню на {plan.week_label} · заготовки впрок")

    if recipes:
        _h2(pdf, fam, "Рецепты")
        for d in plan.dishes:
            pdf.set_font(fam, "B", 13)
            pdf.set_text_color(*INK)
            _mc(pdf, 7, d.name)

            meta = f"Подготовка {d.prep_min} мин · Готовка {d.cook_min} мин · {d.servings} порц."
            if d.storage.freeze:
                meta += f" · Заморозка до {d.storage.shelf_life_days} дн"
            pdf.set_font(fam, "", 9.5)
            pdf.set_text_color(*MUTE)
            _mc(pdf, 5, meta)
            if d.storage.note:
                pdf.set_text_color(*FROST)
                _mc(pdf, 5, f"Хранение: {d.storage.note}")

            if d.ingredients:
                pdf.ln(1)
                pdf.set_font(fam, "B", 9.5)
                pdf.set_text_color(*MUTE)
                _mc(pdf, 5, "Ингредиенты")
                pdf.set_font(fam, "", 10.5)
                pdf.set_text_color(*INK)
                for ing in d.ingredients:
                    _mc(pdf, 5.5, f"•  {ing.name} — {_num(ing.qty)} {ing.unit}")

            if d.steps:
                pdf.ln(1)
                pdf.set_font(fam, "B", 9.5)
                pdf.set_text_color(*MUTE)
                _mc(pdf, 5, "Приготовление")
                pdf.set_font(fam, "", 10.5)
                pdf.set_text_color(*INK)
                for i, step in enumerate(d.steps, 1):
                    _mc(pdf, 5.5, f"{i}.  {step}")

            if d.tips:
                pdf.ln(1)
                pdf.set_font(fam, "", 9.5)
                pdf.set_text_color(*MUTE)
                _mc(pdf, 5, "Советы: " + " ".join(d.tips))
            pdf.ln(3)

    if shop and shopping:
        if recipes:
            pdf.add_page()
        _h2(pdf, fam, "Список покупок")
        for g in shopping:
            pdf.set_font(fam, "B", 10.5)
            pdf.set_text_color(*ACCENT)
            _mc(pdf, 6, g.category)
            pdf.set_font(fam, "", 10.5)
            pdf.set_text_color(*INK)
            for it in g.items:
                _mc(pdf, 5.5, f"•  {it.name} — {_num(it.qty)} {it.unit}")
            pdf.ln(2)

    return bytes(pdf.output())


def _num(qty: float) -> str:
    return str(int(qty)) if qty == int(qty) else str(round(qty, 2))
