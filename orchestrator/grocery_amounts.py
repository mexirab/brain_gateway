"""Add up recipe ingredient amounts for the grocery list.

The meal planner's recipes carry free-text amounts ("1 lb", "2 cans",
"1/2 cup"). When several recipes in a week need the same item, the grocery
list should show the total. The arithmetic happens here, in code, never in
the model: amounts in the same unit are summed ("1 lb" + "1/2 lb" →
"1.5 lb"); anything else is kept as written and joined with " + "
("1 lb + 8 oz", "2 cans + a handful"), so nothing is silently dropped.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Tuple

# Accepted spellings → (canonical singular, plural or None for "never pluralise").
_UNITS: Dict[str, Tuple[str, Optional[str]]] = {}


def _unit(canonical: str, plural: Optional[str], *aliases: str) -> None:
    for a in (canonical, *aliases):
        _UNITS[a] = (canonical, plural)


_unit("lb", None, "lbs", "pound", "pounds")
_unit("oz", None, "ounce", "ounces")
_unit("g", None, "gram", "grams")
_unit("kg", None, "kilogram", "kilograms", "kgs")
_unit("ml", None, "milliliter", "milliliters", "millilitre", "millilitres")
_unit("l", None, "liter", "liters", "litre", "litres")
_unit("tbsp", None, "tablespoon", "tablespoons", "tbsps", "tbs")
_unit("tsp", None, "teaspoon", "teaspoons", "tsps")
for _word in (
    "cup",
    "can",
    "clove",
    "head",
    "jar",
    "bag",
    "package",
    "slice",
    "piece",
    "stalk",
    "sprig",
    "bottle",
    "block",
    "container",
    "fillet",
    "breast",
    "thigh",
    "carton",
    "loaf",
    "pint",
    "quart",
):
    _unit(_word, "loaves" if _word == "loaf" else _word + "s", _word + "s")
_unit("bunch", "bunches", "bunches")
_unit("box", "boxes", "boxes")
_unit("pkg", None)
_UNITS["loaves"] = ("loaf", "loaves")

_VULGAR = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}
_QTY = r"(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|\.\d+)"
_AMOUNT_RE = re.compile(rf"^{_QTY}\s*([a-z]+)?\.?$")
_NUMBERISH = re.compile(r"^[\d\s./]+$")
_MARKUP = re.compile(r"[<>{}\[\]\\`|\x00-\x1f\x7f]")


def _parse(amount: str) -> Optional[Tuple[Fraction, str]]:
    """'1 1/2 cups' → (3/2, 'cup'); '3' → (3, ''). None if it isn't a plain
    number + known unit (e.g. '2 cans (15 oz)', 'a handful', '3 large')."""
    text = amount.strip().lower()
    for sym, frac in _VULGAR.items():
        text = re.sub(rf"(\d)\s*{sym}", rf"\1 {frac}", text).replace(sym, frac)
    m = _AMOUNT_RE.match(text)
    if not m:
        return None
    qty_text, unit_text = m.group(1), m.group(2) or ""
    try:
        if " " in qty_text:
            whole, frac = qty_text.split()
            qty = Fraction(int(whole)) + Fraction(frac)
        else:
            qty = Fraction(qty_text)
    except (ValueError, ZeroDivisionError):
        return None
    if qty <= 0:
        return None
    if unit_text and unit_text not in _UNITS:
        return None
    return qty, (_UNITS[unit_text][0] if unit_text else "")


_PLURAL: Dict[str, Optional[str]] = {c: p for c, p in _UNITS.values()}
_NICE_DENOMINATORS = (2, 3, 4, 8)


def _fmt_qty(qty: Fraction) -> str:
    """Kitchen-style: 1/3 and 1 1/2 for halves, thirds, quarters and eighths;
    otherwise up to two decimals."""
    if qty.denominator == 1:
        return str(qty.numerator)
    if qty.denominator in _NICE_DENOMINATORS:
        whole, rest = divmod(qty.numerator, qty.denominator)
        frac = f"{rest}/{qty.denominator}"
        return f"{whole} {frac}" if whole else frac
    if qty < Fraction(1, 100):
        return "<0.01"
    return f"{float(qty):.2f}".rstrip("0").rstrip(".")


def _fmt(qty: Fraction, unit: str) -> str:
    if not unit:
        return _fmt_qty(qty)
    plural = _PLURAL.get(unit)
    word = plural if plural and qty > 1 else unit
    return f"{_fmt_qty(qty)} {word}"


def combine_amounts(amounts: Iterable[Optional[str]]) -> str:
    """Total a list of free-text amounts. '' when there is nothing to show."""
    totals: Dict[str, Fraction] = {}
    order: List[str] = []  # units and raw strings, in first-seen order
    raw: Dict[str, str] = {}
    for a in amounts:
        if not isinstance(a, str) or not a.strip():
            continue
        parsed = _parse(a)
        if parsed:
            qty, unit = parsed
            key = f"u:{unit}"
            if key not in totals:
                totals[key] = Fraction(0)
                order.append(key)
            totals[key] += qty
        else:
            # Same characters meal_planner._clean_text drops: no markup can ride
            # along into the tool output / plan summary the model reads.
            text = " ".join(_MARKUP.sub(" ", a).split())[:40]
            if not text or _NUMBERISH.match(text):
                continue  # empty, or a number that didn't parse ("0", "1/0"): nothing useful to show
            key = f"r:{text.lower()}"
            if key not in raw:
                raw[key] = text
                order.append(key)
    parts = [_fmt(totals[k], k[2:]) if k.startswith("u:") else raw[k] for k in order]
    return " + ".join(parts)[:80]
