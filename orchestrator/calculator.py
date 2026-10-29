"""Restricted calculator behind the `calculate` tool.

The brain is good at setting a problem up and bad at doing the arithmetic, so
it writes a short program and this module works it out:

    bill = 84.50
    tip = bill * 0.20
    (bill + tip) / 3

Safety model: the program is parsed with `ast.parse`, every node of the tree
is checked against an allow-list (`_validate`) before anything runs, and then
it is interpreted node by node here. Nothing is ever handed to eval/exec.
Names resolve to the program's own variables or the fixed FUNCTIONS /
CONSTANTS tables, and a function can only be called by its bare name. There
are no strings, imports, attribute access, subscripts, loops or function
definitions; variable names are plain ASCII and each assignment has one
target. Without loops every node runs at most once, so the cost of a program
is bounded by its size (MAX_PROGRAM_CHARS, MAX_STATEMENTS, MAX_NODES) and the
per-value caps (MAX_INT_BITS, MAX_LIST_LEN, the factorial / round limits),
which are checked BEFORE an operation that could blow up. The report is built
against MAX_OUTPUT_CHARS as it goes, and very large integers are shown in
short form, so formatting is bounded too.

Medication doses are refused (MEDICATION SAFETY in prompt_builder) when a
variable name contains a dose word or a word of one of the user's own
medication names. This is a soft backstop for the prompt rule, not a
control: bare numbers or neutral names still compute.

Dates and times are local: today()/now() read the same naive `datetime.now()`
as the system prompt's CURRENT DATE/TIME. When `evaluate` is given the local
zone, date-time arithmetic is in real elapsed time, so "hours until 9 am"
across a daylight-saving change is right; date-only math is unaffected.
"""

from __future__ import annotations

import ast
import math
import re
import statistics
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Callable, Iterable, Literal, get_args

MAX_PROGRAM_CHARS = 2000
MAX_STATEMENTS = 40
MAX_NODES = 2500  # secondary bound; MAX_PROGRAM_CHARS is the real one
MAX_DEPTH = 200  # a 150-term a + b + c ... chain nests 150 deep
MAX_INT_BITS = 4096  # ~1233 decimal digits
MAX_LIST_LEN = 200
MAX_FACTORIAL_N = 1000  # factorial / comb / perm argument cap
MAX_ROUND_DIGITS = 20  # round(x, n): |n| cap; int.__round__(-n) builds 10**n
MAX_GCD_ARGS = 20
MAX_OUTPUT_CHARS = 3000
EXACT_INT_BITS = 132  # integers up to ~40 digits are shown exactly, bigger ones in short form

Category = Literal[
    "ok",
    "empty",
    "invalid",
    "not_allowed",
    "too_large",
    "math_error",
    "refused_dose_word",
    "refused_med_name",
]
CATEGORIES: tuple[str, ...] = get_args(Category)

# Variable-name words that mark a program as medication dose math. Left out
# on purpose: "med" (`med = median(xs)`), mg/mcg/milligram (nutrition math
# like sodium_mg), capsule/tablet (coffee capsules, iPads), rx (geometry).
DOSE_WORDS: frozenset[str] = frozenset(
    {
        "dose",
        "doses",
        "dosed",
        "dosage",
        "dosages",
        "dosing",
        "overdose",
        "overdoses",
        "pill",
        "pills",
        "meds",
        "medication",
        "medications",
        "medicine",
        "medicines",
        "prescription",
        "prescriptions",
    }
)

# Everyday words that turn up inside medication and supplement names ("Night
# Time Sleep Aid", "Fish Oil", "Extra Strength"). They are dropped from the
# medication-name tokens, or `leave_time`, `fish = 2` or `sodium_mg` would be
# refused as dose math.
GENERIC_NAME_WORDS: frozenset[str] = frozenset(
    {
        # timing
        "hour",
        "hours",
        "time",
        "times",
        "night",
        "nighttime",
        "days",
        "daily",
        "morning",
        "evening",
        "week",
        "weekly",
        # strength and release
        "extra",
        "strength",
        "extended",
        "release",
        "relief",
        "plus",
        "maximum",
        "regular",
        "original",
        "rapid",
        "fast",
        "acting",
        "long",
        "slow",
        "delayed",
        "sustained",
        "controlled",
        # marketing and audience
        "sleep",
        "aids",
        "care",
        "health",
        "natural",
        "super",
        "ultra",
        "forte",
        "mini",
        "junior",
        "total",
        "free",
        "sugar",
        "formula",
        "complex",
        "multi",
        "children",
        "childrens",
        "kids",
        "adult",
        "adults",
        "women",
        "womens",
        "mens",
        # form
        "chewable",
        "chewables",
        "gummy",
        "gummies",
        "liquid",
        "oral",
        "tablet",
        "tablets",
        "capsule",
        "capsules",
        "softgel",
        "softgels",
        # what it's for
        "cold",
        "sinus",
        "allergy",
        "cough",
        "pain",
        # foods and nutrients (also nutrition math: sodium_mg, protein_g)
        "fish",
        "iron",
        "zinc",
        "vitamin",
        "vitamins",
        "omega",
        "calcium",
        "magnesium",
        "potassium",
        "sodium",
        "protein",
        "fiber",
    }
)

_REFUSAL_TEXT = (
    "calculate refused: the variable names look like a medication dose or dose timing. Don't calculate "
    "doses or dose timing; if that's what was asked, say it's a question for the prescriber."
)
_SYNTAX_HINT = "Write plain numbers (no $, commas or %), * to multiply and ** for powers."

# Characters a model copies from prose that Python doesn't read as operators.
_OPERATOR_ALIASES = str.maketrans({"×": "*", "÷": "/", "−": "-"})
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_LINE_BREAKS = re.compile(r"\r\n|\r|\n")


class CalcError(Exception):
    """A program the calculator won't or can't run. `category` feeds the metric."""

    category: Category = "invalid"

    def __init__(self, message: str, node: ast.AST | None = None):
        super().__init__(message)
        self.line: int | None = getattr(node, "lineno", None)


class ProgramError(CalcError):
    """The program is wrong: unknown name, wrong arguments, mismatched types."""

    category: Category = "invalid"


class NotAllowed(CalcError):
    category: Category = "not_allowed"


class TooLarge(CalcError):
    category: Category = "too_large"


class MathError(CalcError):
    """The program is fine but the math isn't: domain errors, division by zero."""

    category: Category = "math_error"


@dataclass(frozen=True)
class CalcResult:
    ok: bool
    category: Category  # bounded label set for bgw_calculate_total
    text: str  # what the tool returns to the model


Scalar = bool | int | float | date | timedelta  # datetime is a date
Value = Scalar | tuple[Scalar, ...]

# The clock and local zone for one evaluate() call. ContextVars, so a test
# clock never leaks into a concurrent call on another thread.
_CLOCK: ContextVar[Callable[[], datetime]] = ContextVar("calculator_clock", default=datetime.now)
_ZONE: ContextVar[tzinfo | None] = ContextVar("calculator_zone", default=None)


# --------------------------------------------------------------------------- values


def _check(value: Any) -> Value:
    """Admit only the value types this calculator works with, within size caps."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > MAX_INT_BITS:
            raise TooLarge("number too large")
        return value
    if isinstance(value, float):
        if math.isinf(value):  # float overflow: same category as int overflow and exp()
            raise TooLarge("number too large")
        if math.isnan(value):
            raise MathError("result is not a number")
        return value
    if isinstance(value, (date, timedelta)):
        return value
    if isinstance(value, tuple):
        if len(value) > MAX_LIST_LEN:
            raise TooLarge(f"list longer than {MAX_LIST_LEN} items")
        if not all(isinstance(v, (bool, int, float, date, timedelta)) for v in value):
            raise NotAllowed("lists can only hold numbers, dates and durations")
        return value
    if isinstance(value, complex):
        raise MathError("result is not a real number")
    raise NotAllowed(f"values of type {type(value).__name__} are not supported")


def _scalar(value: Value, what: str) -> Scalar:
    if isinstance(value, tuple):
        raise ProgramError(f"{what} works on single values, not lists (use sum/min/max/mean/median)")
    return value


def _number(value: Any, fname: str) -> int | float:
    if isinstance(value, (int, float)):  # bool is an int
        return value
    raise ProgramError(f"{fname}() needs a number")


def _int_arg(value: Any, fname: str) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return value
    raise ProgramError(f"{fname}() needs a whole number")


def _items(args: tuple, fname: str) -> list:
    """min(a, b) and min([a, b]) both work, like Python's builtins."""
    items = list(args[0]) if len(args) == 1 and isinstance(args[0], tuple) else list(args)
    if any(isinstance(a, tuple) for a in items):
        raise ProgramError(f"{fname}() takes numbers or one list, not nested lists")
    if not items:
        raise ProgramError(f"{fname}() needs at least one value")
    return items


def _is_plain_date(v: Any) -> bool:
    return isinstance(v, date) and not isinstance(v, datetime)


def _to_utc(dt: datetime, zone: tzinfo) -> datetime:
    return dt.replace(tzinfo=zone).astimezone(UTC)


def _from_utc(dt: datetime, zone: tzinfo) -> datetime:
    return dt.astimezone(zone).replace(tzinfo=None)


def _pow_precheck(base: Any, exponent: Any) -> None:
    """Refuse an integer power whose result would pass MAX_INT_BITS before
    Python spends the time and memory building it.

    (bit_length - 1) * exponent is a lower bound on the result's size, so this
    never refuses a result that fits; anything it lets through is at most
    about twice the cap (base 2 or 3) and _check rejects it afterwards.
    """
    if (
        isinstance(base, int)
        and isinstance(exponent, int)
        and exponent > 0
        and base not in (-1, 0, 1)
        and (abs(base).bit_length() - 1) * exponent > MAX_INT_BITS
    ):
        raise TooLarge("number too large")


def _power(base: Any, exponent: Any) -> Any:
    _pow_precheck(base, exponent)
    return base**exponent


def _arith(op: type[ast.operator], a: Scalar, b: Scalar) -> Any:
    """One arithmetic step, with date/time rules on top of Python's."""
    if op in (ast.Add, ast.Sub):
        # A plain date next to a date-time counts as midnight ("hours until Christmas").
        if _is_plain_date(a) and isinstance(b, datetime):
            a = datetime.combine(a, time())
        elif _is_plain_date(b) and isinstance(a, datetime):
            b = datetime.combine(b, time())
        # Python's date + timedelta silently drops hours and minutes.
        for d, other in ((a, b), (b, a)):
            if _is_plain_date(d) and isinstance(other, timedelta) and (other.seconds or other.microseconds):
                raise ProgramError(
                    "today() and date() have no time of day; use now(), at(hour, minute) or "
                    "datetime(y, m, d, hour, minute) for hours and minutes"
                )
        zone = _ZONE.get()
        if zone is not None:  # real elapsed time across daylight-saving changes
            if op is ast.Sub and isinstance(a, datetime) and isinstance(b, datetime):
                return _to_utc(a, zone) - _to_utc(b, zone)
            if isinstance(a, datetime) and isinstance(b, timedelta):
                moved = _to_utc(a, zone) + (b if op is ast.Add else -b)
                return _from_utc(moved, zone)
            if op is ast.Add and isinstance(a, timedelta) and isinstance(b, datetime):
                return _from_utc(_to_utc(b, zone) + a, zone)
        return a + b if op is ast.Add else a - b
    if op is ast.Mult:
        return a * b
    if op is ast.Div:
        return a / b
    if op is ast.FloorDiv:
        return a // b
    if op is ast.Mod:
        return a % b
    if op is ast.Pow:
        return _power(a, b)
    raise NotAllowed("operator not available")


def _compare(op: type[ast.cmpop], a: Scalar, b: Scalar) -> bool:
    if _is_plain_date(a) and isinstance(b, datetime):
        a = datetime.combine(a, time())
    elif _is_plain_date(b) and isinstance(a, datetime):
        b = datetime.combine(b, time())
    if op is ast.Eq:
        return a == b
    if op is ast.NotEq:
        return a != b
    if op is ast.Lt:
        return a < b
    if op is ast.LtE:
        return a <= b
    if op is ast.Gt:
        return a > b
    if op is ast.GtE:
        return a >= b
    raise NotAllowed("comparison not available")


# --------------------------------------------------------------------------- functions


def _round(x: Any, ndigits: Any = None) -> int | float:
    """Round halves up (away from zero), the way people round money:
    round(2.5) is 3 and round(2.675, 2) is 2.68, unlike Python's round()."""
    x = _number(x, "round")
    n = 0 if ndigits is None else _int_arg(ndigits, "round")
    if abs(n) > MAX_ROUND_DIGITS:
        raise TooLarge(f"round() digits must be between -{MAX_ROUND_DIGITS} and {MAX_ROUND_DIGITS}")
    if isinstance(x, int):
        if n >= 0:
            return int(x)
        step = 10**-n
        q, r = divmod(abs(x), step)
        if 2 * r >= step:
            q += 1
        return q * step if x >= 0 else -q * step
    with localcontext() as ctx:
        ctx.prec = 400  # room for a 1e308 float rounded to 20 places
        rounded = Decimal(repr(x)).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)
    return int(rounded) if ndigits is None else float(rounded)


def _factorial(n: Any) -> int:
    n = _int_arg(n, "factorial")
    if n > MAX_FACTORIAL_N:
        raise TooLarge(f"factorial() is limited to n <= {MAX_FACTORIAL_N}")
    return math.factorial(n)


def _comb(n: Any, k: Any) -> int:
    n, k = _int_arg(n, "comb"), _int_arg(k, "comb")
    if n > MAX_FACTORIAL_N:
        raise TooLarge(f"comb() is limited to n <= {MAX_FACTORIAL_N}")
    return math.comb(n, k)


def _perm(n: Any, k: Any = None) -> int:
    n = _int_arg(n, "perm")
    if n > MAX_FACTORIAL_N:
        raise TooLarge(f"perm() is limited to n <= {MAX_FACTORIAL_N}")
    return math.perm(n) if k is None else math.perm(n, _int_arg(k, "perm"))


def _gcd_lcm(fname: str, fn: Callable[[int, int], int]) -> Callable[..., int]:
    def run(*args: Any) -> int:
        items = [_int_arg(a, fname) for a in _items(args, fname)]
        if len(items) > MAX_GCD_ARGS:
            raise TooLarge(f"{fname}() takes at most {MAX_GCD_ARGS} numbers")
        acc = items[0]
        for v in items[1:]:
            acc = _check(fn(acc, v))  # lcm grows: cap every step, not just the end
        return acc

    return run


def _sum(*args: Any) -> Any:
    items = _items(args, "sum")
    acc = items[0]
    for v in items[1:]:
        acc = _check(_arith(ast.Add, acc, v))
    return acc


def _mean(*args: Any) -> Any:
    items = _items(args, "mean")
    if all(isinstance(v, timedelta) for v in items):  # "average of 25, 31 and 40 minutes"
        return sum(items, timedelta()) / len(items)
    numbers = [_number(v, "mean") for v in items]
    return math.fsum(numbers) / len(numbers)


def _median(*args: Any) -> Any:
    return statistics.median(_items(args, "median"))


def _minmax(fname: str, fn: Callable[[list], Any]) -> Callable[..., Any]:
    return lambda *args: fn(_items(args, fname))


def _unary_math(fname: str, fn: Callable[[float], Any]) -> Callable[[Any], Any]:
    return lambda x: fn(_number(x, fname))


def _abs(x: Any) -> Any:
    return abs(x) if isinstance(x, timedelta) else abs(_number(x, "abs"))


def _log(x: Any, base: Any = math.e) -> float:
    return math.log(_number(x, "log"), _number(base, "log"))


def _hypot(*args: Any) -> float:
    return math.hypot(*[_number(v, "hypot") for v in _items(args, "hypot")])


def _today() -> date:
    return _CLOCK.get()().date()


def _now() -> datetime:
    return _CLOCK.get()().replace(second=0, microsecond=0)


def _date(y: Any, m: Any, d: Any) -> date:
    return date(_int_arg(y, "date"), _int_arg(m, "date"), _int_arg(d, "date"))


def _datetime(y: Any, m: Any, d: Any, hour: Any = 0, minute: Any = 0) -> datetime:
    return datetime(*[_int_arg(v, "datetime") for v in (y, m, d, hour, minute)])


def _at(hour: Any, minute: Any = 0) -> datetime:
    return _now().replace(hour=_int_arg(hour, "at"), minute=_int_arg(minute, "at"))


def _duration(unit: str) -> Callable[[Any], timedelta]:
    return lambda n: timedelta(**{unit: _number(n, unit)})


def _in_unit(fname: str, seconds: int) -> Callable[[Any], float]:
    def run(x: Any) -> float:
        if not isinstance(x, timedelta):
            raise ProgramError(f"{fname}() needs a duration, e.g. {fname}(date(2026, 12, 25) - today())")
        return x.total_seconds() / seconds

    return run


_NO_KWARGS: frozenset[str] = frozenset()

# name -> (callable, allowed keyword names)
FUNCTIONS: dict[str, tuple[Callable[..., Any], frozenset[str]]] = {
    "abs": (_abs, _NO_KWARGS),
    "round": (_round, frozenset({"ndigits"})),
    "int": (_unary_math("int", int), _NO_KWARGS),
    "float": (_unary_math("float", float), _NO_KWARGS),
    "floor": (_unary_math("floor", math.floor), _NO_KWARGS),
    "ceil": (_unary_math("ceil", math.ceil), _NO_KWARGS),
    "sqrt": (_unary_math("sqrt", math.sqrt), _NO_KWARGS),
    "cbrt": (_unary_math("cbrt", math.cbrt), _NO_KWARGS),
    "pow": (lambda a, b: _power(_number(a, "pow"), _number(b, "pow")), _NO_KWARGS),
    "exp": (_unary_math("exp", math.exp), _NO_KWARGS),
    "ln": (_unary_math("ln", math.log), _NO_KWARGS),
    "log": (_log, frozenset({"base"})),
    "log10": (_unary_math("log10", math.log10), _NO_KWARGS),
    "log2": (_unary_math("log2", math.log2), _NO_KWARGS),
    "sin": (_unary_math("sin", math.sin), _NO_KWARGS),
    "cos": (_unary_math("cos", math.cos), _NO_KWARGS),
    "tan": (_unary_math("tan", math.tan), _NO_KWARGS),
    "asin": (_unary_math("asin", math.asin), _NO_KWARGS),
    "acos": (_unary_math("acos", math.acos), _NO_KWARGS),
    "atan": (_unary_math("atan", math.atan), _NO_KWARGS),
    "degrees": (_unary_math("degrees", math.degrees), _NO_KWARGS),
    "radians": (_unary_math("radians", math.radians), _NO_KWARGS),
    "hypot": (_hypot, _NO_KWARGS),
    "factorial": (_factorial, _NO_KWARGS),
    "comb": (_comb, _NO_KWARGS),
    "perm": (_perm, _NO_KWARGS),
    "gcd": (_gcd_lcm("gcd", math.gcd), _NO_KWARGS),
    "lcm": (_gcd_lcm("lcm", math.lcm), _NO_KWARGS),
    "min": (_minmax("min", min), _NO_KWARGS),
    "max": (_minmax("max", max), _NO_KWARGS),
    "sum": (_sum, _NO_KWARGS),
    "mean": (_mean, _NO_KWARGS),
    "median": (_median, _NO_KWARGS),
    "today": (_today, _NO_KWARGS),
    "now": (_now, _NO_KWARGS),
    "date": (_date, _NO_KWARGS),
    "datetime": (_datetime, frozenset({"hour", "minute"})),
    "at": (_at, frozenset({"minute"})),
    "days": (_duration("days"), _NO_KWARGS),
    "weeks": (_duration("weeks"), _NO_KWARGS),
    "hours": (_duration("hours"), _NO_KWARGS),
    "minutes": (_duration("minutes"), _NO_KWARGS),
    "in_days": (_in_unit("in_days", 86400), _NO_KWARGS),
    "in_hours": (_in_unit("in_hours", 3600), _NO_KWARGS),
    "in_minutes": (_in_unit("in_minutes", 60), _NO_KWARGS),
}

CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}


# --------------------------------------------------------------------------- validation

_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_ALLOWED_NODES = (
    (ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.Name, ast.Load, ast.Store, ast.Constant)
    + (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.List, ast.Call, ast.keyword)
    + (ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or)
    + _BINOPS
    + _CMPOPS
)

_REFUSALS: tuple[tuple[tuple[type, ...], str], ...] = (
    ((ast.Import, ast.ImportFrom), "imports are not available; the math functions are built in"),
    ((ast.For, ast.AsyncFor, ast.While), "loops are not available; write the steps out, or use sum/min/max/mean"),
    ((ast.If,), "if statements are not available; use: a if condition else b"),
    (
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Return),
        "defining functions is not available",
    ),
    (
        (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
        "comprehensions are not available; write the list out",
    ),
    ((ast.Attribute,), "attribute access is not available; use in_days(x), in_hours(x) or in_minutes(x)"),
    ((ast.Subscript, ast.Slice), "indexing is not available"),
    (
        (ast.Tuple,),
        "commas separate list items: write 1250 (or 1_250), not 1,250; use [a, b] for a list "
        "and one assignment per line",
    ),
    ((ast.Dict, ast.Set), "dictionaries and sets are not available; use [a, b] for a list"),
    ((ast.JoinedStr, ast.FormattedValue), "text strings are not available; use numbers, dates and durations"),
    ((ast.NamedExpr,), "the := operator is not available; put the assignment on its own line"),
    ((ast.Starred,), "*args are not available"),
    ((ast.BitXor,), "^ is not a power here; use ** (2 ** 10) or pow(2, 10)"),
    (
        (ast.BitAnd, ast.BitOr, ast.LShift, ast.RShift, ast.MatMult, ast.Invert),
        "bitwise and matrix operators are not available",
    ),
    ((ast.In, ast.NotIn, ast.Is, ast.IsNot), "only == != < <= > >= comparisons are available"),
)


def _refusal(node: ast.AST) -> NotAllowed:
    for types, message in _REFUSALS:
        if isinstance(node, types):
            return NotAllowed(message, node)
    return NotAllowed(f"'{type(node).__name__}' is not available", node)


def _validate(tree: ast.Module) -> None:
    """Refuse the whole program up front if any node is outside the language,
    so the outcome never depends on which branch would have run."""
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise _refusal(node)
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, (str, bytes)):
                raise NotAllowed("text strings are not available; use numbers, dates and durations", node)
            if not isinstance(v, (bool, int, float)):
                raise NotAllowed(f"constant {type(v).__name__} is not available", node)
        elif isinstance(node, ast.Name):
            if not _NAME_RE.fullmatch(node.id):
                raise NotAllowed(
                    "variable names must be plain letters, digits and _ (start with a letter, at most 32 characters)",
                    node,
                )
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise NotAllowed("assign one plain name per line, e.g. total = a + b", node)
        elif isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name):
                raise NotAllowed("assign one plain name per line, e.g. total += 5", node)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise NotAllowed("only the built-in functions can be called, by name, e.g. sqrt(2)", node)
            entry = FUNCTIONS.get(node.func.id)
            if entry is None:
                raise ProgramError(f"there is no function '{node.func.id}'", node)
            for kw in node.keywords:
                if kw.arg is None:
                    raise NotAllowed("**kwargs are not available", node)
                if kw.arg not in entry[1]:
                    raise ProgramError(f"{node.func.id}() has no '{kw.arg}' option", node)
            if len(node.args) + len(node.keywords) > MAX_LIST_LEN:
                raise TooLarge("too many arguments", node)
        elif isinstance(node, ast.List):
            if len(node.elts) > MAX_LIST_LEN:
                raise TooLarge(f"list longer than {MAX_LIST_LEN} items", node)
            if any(isinstance(e, ast.List) for e in node.elts):
                raise NotAllowed("lists can't contain lists", node)


# --------------------------------------------------------------------------- interpreter


class _Interpreter:
    def __init__(self) -> None:
        self.variables: dict[str, Value] = {}
        self.line: int | None = None

    def run(self, tree: ast.Module) -> Value:
        last: Value = 0
        for stmt in tree.body:
            self.line = stmt.lineno
            last = self._statement(stmt)
        return last

    def _statement(self, node: ast.stmt) -> Value:
        if isinstance(node, ast.Expr):
            return self._eval(node.value, 0)
        if isinstance(node, ast.Assign):
            target = node.targets[0]
            assert isinstance(target, ast.Name)  # _validate guarantees it
            value = self._eval(node.value, 0)
            self.variables[target.id] = value
            return value
        if isinstance(node, ast.AugAssign):
            assert isinstance(node.target, ast.Name)
            name = node.target.id
            if name not in self.variables:
                raise ProgramError(f"'{name}' has no value yet")
            left = _scalar(self.variables[name], "arithmetic")
            right = _scalar(self._eval(node.value, 0), "arithmetic")
            value = _check(_arith(type(node.op), left, right))
            self.variables[name] = value
            return value
        raise _refusal(node)

    def _eval(self, node: ast.expr, depth: int) -> Value:
        if depth > MAX_DEPTH:
            raise NotAllowed("expression is nested too deeply; for a long list of numbers use sum([...])")
        d = depth + 1

        if isinstance(node, ast.Constant):
            return _check(node.value)

        if isinstance(node, ast.Name):
            if node.id in self.variables:
                return self.variables[node.id]
            if node.id in CONSTANTS:
                return CONSTANTS[node.id]
            if node.id in FUNCTIONS:
                raise ProgramError(f"'{node.id}' is a function; call it like {node.id}(...)")
            raise ProgramError(f"unknown name '{node.id}'")

        if isinstance(node, ast.BinOp):
            left = _scalar(self._eval(node.left, d), "arithmetic")
            right = _scalar(self._eval(node.right, d), "arithmetic")
            return _check(_arith(type(node.op), left, right))

        if isinstance(node, ast.UnaryOp):
            v = _scalar(self._eval(node.operand, d), "arithmetic")
            if isinstance(node.op, ast.Not):
                return not v
            if isinstance(node.op, ast.USub):
                return _check(-v)
            return _check(+v)

        if isinstance(node, ast.BoolOp):
            # Python semantics: `and` stops at the first falsy value, `or` at the first truthy one.
            is_and = isinstance(node.op, ast.And)
            result: Value = False
            for operand in node.values:
                result = _scalar(self._eval(operand, d), "and/or")
                if bool(result) != is_and:
                    return result
            return result

        if isinstance(node, ast.Compare):
            left = _scalar(self._eval(node.left, d), "comparison")
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                right = _scalar(self._eval(comparator, d), "comparison")
                if not _compare(type(op), left, right):
                    return False
                left = right
            return True

        if isinstance(node, ast.IfExp):
            test = _scalar(self._eval(node.test, d), "if/else")
            return self._eval(node.body if test else node.orelse, d)

        if isinstance(node, ast.List):
            return _check(tuple(_scalar(self._eval(e, d), "a list item") for e in node.elts))

        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name)
            name = node.func.id
            fn, _ = FUNCTIONS[name]
            args = [self._eval(a, d) for a in node.args]
            kwargs = {kw.arg: self._eval(kw.value, d) for kw in node.keywords if kw.arg is not None}
            try:
                return _check(fn(*args, **kwargs))
            except TypeError as e:
                if "argument" in str(e):  # arity errors name our internal lambdas
                    raise ProgramError(f"{name}() was called with the wrong number of arguments") from None
                raise ProgramError(f"{name}(): {e}") from None

        raise _refusal(node)


# --------------------------------------------------------------------------- formatting


def _fmt_int(v: int) -> str:
    if v.bit_length() <= EXACT_INT_BITS:
        return str(v)
    # Short form, without building the full decimal string.
    a = abs(v)
    lg = math.log10(a)
    digits = math.floor(lg) + 1
    # log10 is a float, so next to a power of ten it can land on the wrong
    # side (log10(10**50 - 1) == 50.0); settle the digit count exactly.
    if a < 10 ** (digits - 1):
        digits -= 1
    elif a >= 10**digits:
        digits += 1
    exponent = digits - 1
    mantissa = 10 ** (lg - exponent)
    if mantissa >= 9.9999995:  # shows as 10.000000: move the point, not the digit count
        mantissa, exponent = 1.0, exponent + 1
    return f"{'-' if v < 0 else ''}≈{mantissa:.6f}e+{exponent} ({digits} digits)"


def _fmt_number(v: int | float) -> str:
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return _fmt_int(v)
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return format(v, ".12g")


def _plural(n: int | float, unit: str) -> str:
    return f"{_fmt_number(n)} {unit}{'' if n == 1 else 's'}"


def _fmt_duration(td: timedelta) -> str:
    total_ms = round(td.total_seconds() * 1000)
    sign = "-" if total_ms < 0 else ""
    days, rem = divmod(abs(total_ms), 86_400_000)
    hours, rem = divmod(rem, 3_600_000)
    minutes, ms = divmod(rem, 60_000)
    parts = [_plural(n, unit) for n, unit in ((days, "day"), (hours, "hour"), (minutes, "minute")) if n]
    if ms:
        parts.append(_plural(ms / 1000, "second"))
    if not parts:
        return "0 minutes"
    text = " ".join(parts)
    text = f"-({text})" if sign and len(parts) > 1 else sign + text
    secs = abs(total_ms) / 1000
    if days and (hours or minutes):
        text += f" (= {sign}{_fmt_number(round(secs / 3600, 3))} hours)"
    elif hours and minutes:
        text += f" (= {sign}{_fmt_number(round(secs / 60, 3))} minutes)"
    return text


def format_value(v: Value, budget: int = MAX_OUTPUT_CHARS) -> str:
    """One value as text; a list stops adding items once past `budget` chars."""
    if isinstance(v, datetime):
        twelve = v.strftime("%I:%M %p").lstrip("0")
        return f"{v:%Y-%m-%d %H:%M} ({twelve}, {v:%A})"
    if isinstance(v, date):
        return f"{v:%Y-%m-%d} ({v:%A})"
    if isinstance(v, timedelta):
        return _fmt_duration(v)
    if isinstance(v, tuple):
        parts: list[str] = []
        used = 2
        for item in v:
            s = format_value(item)
            if used + len(s) > budget:
                parts.append("…")
                break
            parts.append(s)
            used += len(s) + 2
        return "[" + ", ".join(parts) + "]"
    return _fmt_number(v)


def _report(result: Value, variables: dict[str, Value], show_variables: bool) -> str:
    lines = [f"result: {format_value(result)}"]
    used = len(lines[0])
    if show_variables:
        for name, value in variables.items():
            remaining = MAX_OUTPUT_CHARS - used
            if remaining <= len(name) + 10:
                lines.append("… (more variables not shown)")
                break
            line = f"{name} = {format_value(value, remaining - len(name) - 3)}"
            lines.append(line)
            used += len(line) + 1
    return "\n".join(lines)


# --------------------------------------------------------------------------- entry point

_LETTERS = re.compile(r"[a-z]+")
_CAMEL = re.compile(r"([a-z])([A-Z])")


def _name_tokens(name: str) -> set[str]:
    """Words of a variable name: split on _ and digits, both as written and
    split at camelCase humps, so `dailyDose`, `DoSE` and `dose_mg` all yield
    "dose"."""
    return set(_LETTERS.findall(name.lower())) | set(_LETTERS.findall(_CAMEL.sub(r"\1_\2", name).lower()))


def medication_name_tokens(meds: object) -> frozenset[str]:
    """Distinctive words (4+ letters, not GENERIC_NAME_WORDS) of the user's
    medication names, from a data_manager.get_medications() dict."""
    names: list[str] = []

    def collect(items: Iterable[Any]) -> None:
        for m in items:
            if isinstance(m, dict) and isinstance(m.get("name"), str):
                names.append(m["name"])

    if isinstance(meds, dict):
        daily = meds.get("daily")
        if isinstance(daily, dict):
            for bucket in daily.values():
                if isinstance(bucket, list):
                    collect(bucket)
        for key in ("weekly", "as_needed"):
            if isinstance(meds.get(key), list):
                collect(meds[key])
    # Fold accents ("Méthylphénidate" -> "methylphenidate"): variable names are ASCII.
    folded = [unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower() for n in names]
    return frozenset(t for n in folded for t in _LETTERS.findall(n) if len(t) >= 4 and t not in GENERIC_NAME_WORDS)


def _where(line: int | None) -> str:
    return f" (line {line})" if line else ""


def evaluate(
    program: str,
    *,
    med_tokens: frozenset[str] = frozenset(),
    dose_words: frozenset[str] = DOSE_WORDS,
    clock: Callable[[], datetime] | None = None,
    zone: tzinfo | None = None,
) -> CalcResult:
    """Run `program` and describe the outcome for the model. Never raises.

    `zone` is the local time zone; with it, date-time arithmetic is in real
    elapsed time. `clock` replaces datetime.now for tests.
    """
    if not program or not program.strip():
        return CalcResult(False, "empty", "calculate error: the program is empty")
    if len(program) > MAX_PROGRAM_CHARS:
        return CalcResult(False, "too_large", f"calculate error: program longer than {MAX_PROGRAM_CHARS} characters")

    # The language has no blocks, so stripping each line is safe: it accepts
    # indented programs and keeps line numbers matching the model's lines.
    # Split only where Python's tokenizer breaks lines: str.splitlines() also
    # splits on \f, \v and others, which would turn "1\f+ 1" into "1".
    lines = _LINE_BREAKS.split(program.translate(_OPERATOR_ALIASES))
    source = "\n".join(line.strip() for line in lines)
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as e:
        # e.msg describes the model's own program; it is returned, never logged.
        return CalcResult(False, "invalid", f"calculate error{_where(e.lineno)}: {e.msg}. {_SYNTAX_HINT}")
    except (ValueError, RecursionError, MemoryError, OverflowError):
        return CalcResult(False, "invalid", f"calculate error: not valid calculator syntax. {_SYNTAX_HINT}")

    if not tree.body:
        return CalcResult(False, "empty", "calculate error: the program has no statements (only comments)")
    if len(tree.body) > MAX_STATEMENTS:
        return CalcResult(False, "too_large", f"calculate error: more than {MAX_STATEMENTS} lines")
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        return CalcResult(False, "too_large", "calculate error: program too long; split it into smaller steps")

    try:
        _validate(tree)
    except CalcError as e:
        return CalcResult(False, e.category, f"calculate error{_where(e.line)}: {e}")

    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in FUNCTIONS:
            tokens |= _name_tokens(node.id)
    if tokens & dose_words:
        return CalcResult(False, "refused_dose_word", _REFUSAL_TEXT)
    if tokens & med_tokens:
        return CalcResult(False, "refused_med_name", _REFUSAL_TEXT)

    interp = _Interpreter()
    clock_token = _CLOCK.set(clock) if clock is not None else None
    zone_token = _ZONE.set(zone)
    try:
        result = interp.run(tree)
        single_assignment = len(tree.body) == 1 and isinstance(tree.body[0], (ast.Assign, ast.AugAssign))
        text = _report(result, interp.variables, show_variables=not single_assignment)
    except CalcError as e:
        return CalcResult(False, e.category, f"calculate error{_where(e.line or interp.line)}: {e}")
    except ZeroDivisionError:
        return CalcResult(False, "math_error", f"calculate error{_where(interp.line)}: division by zero")
    except OverflowError:
        return CalcResult(False, "too_large", f"calculate error{_where(interp.line)}: number or date out of range")
    except (ArithmeticError, ValueError) as e:
        return CalcResult(False, "math_error", f"calculate error{_where(interp.line)}: {e}")
    except TypeError as e:
        return CalcResult(False, "invalid", f"calculate error{_where(interp.line)}: can't combine these values ({e})")
    except (RecursionError, MemoryError):
        return CalcResult(False, "too_large", "calculate error: program too complex")
    except Exception:  # noqa: BLE001 — the contract is "never raises"; no detail echoed
        return CalcResult(False, "invalid", f"calculate error{_where(interp.line)}: could not run this program")
    finally:
        _ZONE.reset(zone_token)
        if clock_token is not None:
            _CLOCK.reset(clock_token)
    return CalcResult(True, "ok", text)
