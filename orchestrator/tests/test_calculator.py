"""
Tests for the `calculate` tool (orchestrator/calculator.py + its handler).

- arithmetic, variables, rounding and the result/variable report;
- dates, times and durations on an injected clock, incl. real elapsed time
  across a daylight-saving change;
- every construct outside the calculator's language is refused up front, with
  the category pinned per case (the metric depends on it);
- size caps refuse programs that would burn CPU or memory, before doing so;
- the output budget holds against the chained-alias amplification found in
  review;
- dose math is refused by dose words and by the user's own medication names,
  everyday words in med names don't cause false refusals, and the refusal
  never tells the model to recite the medications;
- the handler counts outcomes, never logs content, the schema is exposed in
  chat and voice, the tool is non-terminal, and the MATH rule sits in the
  static prompt.
"""

import logging
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orchestrator import calculator, prompt_builder, tool_definitions, tool_handlers
from orchestrator.calculator import CATEGORIES, evaluate, medication_name_tokens
from orchestrator.metrics import CALCULATE_TOTAL
from orchestrator.tool_registry import execute_tool
from orchestrator.unified_loop import TERMINAL_TOOLS

CLOCK = lambda: datetime(2026, 10, 25, 16, 42, 13)  # noqa: E731 — a Sunday
CHICAGO = ZoneInfo("America/Chicago")  # US clocks fall back 2026-11-01 02:00


def run(program: str, **kw) -> calculator.CalcResult:
    kw.setdefault("clock", CLOCK)
    return evaluate(program, **kw)


def ok_text(program: str, **kw) -> str:
    r = run(program, **kw)
    assert r.ok, r.text
    assert r.category == "ok"
    return r.text


# --------------------------------------------------------------------------- arithmetic


@pytest.mark.parametrize(
    "program, expected",
    [
        ("0.1 + 0.2", "result: 0.3"),
        ("18 * 64 / 100", "result: 11.52"),
        ("7 // 2", "result: 3"),
        ("7 % 3", "result: 1"),
        ("2 ** 10", "result: 1024"),
        ("pow(2, 10)", "result: 1024"),
        ("-(3 - 5)", "result: 2"),
        ("sqrt(2)", "result: 1.41421356237"),
        ("cbrt(27)", "result: 3"),
        ("ln(e)", "result: 1"),
        ("log(8, 2)", "result: 3"),
        ("log(100, base=10)", "result: 2"),
        ("factorial(20)", "result: 2432902008176640000"),
        ("comb(5, 2)", "result: 10"),
        ("gcd(12, 18)", "result: 6"),
        ("lcm(4, 6)", "result: 12"),
        ("mean([3, 4, 8])", "result: 5"),
        ("median(3, 1, 2, 10)", "result: 2.5"),
        ("max(4, 9, 2)", "result: 9"),
        ("min([4, 9, 2])", "result: 2"),
        ("sum([12.5, 3.2, 8])", "result: 23.7"),
        ("hypot(3, 4)", "result: 5"),
        ("round(1000 * (1 + 0.05 / 12) ** (12 * 10), 2)", "result: 1647.01"),
        ("3 < 5 <= 5", "result: True"),
        ("0 or 7", "result: 7"),
        ("1 and 0", "result: 0"),
        ("0 and 1 / 0", "result: 0"),  # short-circuit skips the error
        ("1 or 1 / 0", "result: 1"),
        ("10 if 3 > 2 else 20", "result: 10"),
        ("int(7.9) + float(1)", "result: 8"),
        ("degrees(pi)", "result: 180"),
        ("1_250 * 2", "result: 2500"),
        ("84.50 × 0.2", "result: 16.9"),  # ×
        ("10 − 3", "result: 7"),  # − (minus sign)
        ("9 ÷ 2", "result: 4.5"),  # ÷
    ],
)
def test_arithmetic(program, expected):
    assert ok_text(program) == expected


@pytest.mark.parametrize(
    "program, expected",
    [
        ("round(2.5)", "result: 3"),
        ("round(3.5)", "result: 4"),
        ("round(-2.5)", "result: -3"),
        ("round(2.675, 2)", "result: 2.68"),
        ("round(0.125, 2)", "result: 0.13"),
        ("round(2 / 3, 4)", "result: 0.6667"),
        ("round(1250, -2)", "result: 1300"),
        ("round(1249, -2)", "result: 1200"),
        ("round(-1250, -2)", "result: -1300"),
        ("round(7)", "result: 7"),
        ("round(1e308, 20)", "result: 1e+308"),
    ],
)
def test_round_halves_up(program, expected):
    assert ok_text(program) == expected


def test_bill_split_reports_every_variable():
    text = ok_text("bill = 84.50\ntip = bill * 0.20\n(bill + tip) / 3")
    assert text.splitlines() == ["result: 33.8", "bill = 84.5", "tip = 16.9"]


def test_single_assignment_reports_only_the_result():
    assert ok_text("x = 2 * 21") == "result: 42"


def test_augmented_assignment():
    assert ok_text("x = 5\nx += 2\nx * 3").splitlines() == ["result: 21", "x = 7"]


def test_function_names_can_be_variable_names():
    # Calls always resolve to the built-in, so shadowing is safe and natural.
    assert ok_text("hours = 37.5\nrate = 22\nhours * rate").splitlines()[0] == "result: 825"
    assert ok_text("hours = 2\nhours(hours)").splitlines()[0] == "result: 2 hours"
    assert ok_text("e = 0.05\n1000 * (1 + e)").splitlines()[0] == "result: 1050"
    assert ok_text("pi = 3\npi * 2").splitlines()[0] == "result: 6"


def test_indented_program_and_comments():
    assert ok_text("  a = 1\n    b = 2  # two\n  a + b").splitlines()[0] == "result: 3"


def test_long_addition_chain_is_allowed():
    assert ok_text("+".join(["1"] * 150)) == "result: 150"


def test_big_integers_are_shown_in_short_form():
    assert ok_text("2 ** 100") == "result: 1267650600228229401496703205376"
    assert ok_text("2 ** 4000") == "result: ≈1.318204e+1204 (1205 digits)"
    assert ok_text("-(10 ** 50)") == "result: -≈1.000000e+50 (51 digits)"
    assert run("factorial(450)").ok


def test_short_form_digit_count_is_exact_next_to_powers_of_ten():
    # 50 nines show as ≈1.000000e+50 (the mantissa rounds up), but they are
    # still 50 digits, and float log10(10**50 - 1) is exactly 50.0. The count
    # used to come out one too high for every short-form 99…9-style number.
    assert ok_text("10 ** 50 - 1") == "result: ≈1.000000e+50 (50 digits)"
    assert ok_text("-(10 ** 100 - 1)") == "result: -≈1.000000e+100 (100 digits)"
    for k in range(40, 1234):  # every power of ten from the first short-form one to MAX_INT_BITS
        for program, digits in ((f"10 ** {k} - 1", k), (f"10 ** {k}", k + 1)):
            text = ok_text(program)
            assert text.endswith(f"({digits} digits)"), (program, text)


def test_list_result_shows_every_item_in_its_own_format():
    assert ok_text("[1, 2.5, hours(1) + minutes(30), today(), 2 > 3]") == (
        "result: [1, 2.5, 1 hour 30 minutes (= 90 minutes), 2026-10-25 (Sunday), False]"
    )


# --------------------------------------------------------------------------- dates and times


@pytest.mark.parametrize(
    "program, expected",
    [
        ("at(18, 15) - minutes(25)", "result: 2026-10-25 17:50 (5:50 PM, Sunday)"),
        ("now()", "result: 2026-10-25 16:42 (4:42 PM, Sunday)"),
        ("today()", "result: 2026-10-25 (Sunday)"),
        ("today() + weeks(3)", "result: 2026-11-15 (Sunday)"),
        ("date(2026, 12, 25) - today()", "result: 61 days"),
        ("in_days(date(2026, 12, 25) - today())", "result: 61"),
        ("date(2026, 10, 27) - now()", "result: 1 day 7 hours 18 minutes (= 31.3 hours)"),
        ("now() - today()", "result: 16 hours 42 minutes (= 1002 minutes)"),
        ("now() > today()", "result: True"),
        ("hours(2) / minutes(25)", "result: 4.8"),
        ("hours(1) + minutes(35)", "result: 1 hour 35 minutes (= 95 minutes)"),
        ("days(1) + hours(2)", "result: 1 day 2 hours (= 26 hours)"),
        ("minutes(10) - minutes(35)", "result: -25 minutes"),
        ("days(-1) - hours(2)", "result: -(1 day 2 hours) (= -26 hours)"),
        ("days(-2) - minutes(0.5)", "result: -(2 days 30 seconds)"),
        ("minutes(1 / 60)", "result: 1 second"),
        ("minutes(0.99999999)", "result: 1 minute"),
        ("abs(minutes(-5))", "result: 5 minutes"),
        ("in_minutes(datetime(2026, 10, 25, 18, 0) - now())", "result: 78"),
        ("datetime(2026, 1, 1, hour=9)", "result: 2026-01-01 09:00 (9:00 AM, Thursday)"),
        ("at(0, 30)", "result: 2026-10-25 00:30 (12:30 AM, Sunday)"),
        ("at(12, 0)", "result: 2026-10-25 12:00 (12:00 PM, Sunday)"),
        ("sum([hours(1), minutes(30)])", "result: 1 hour 30 minutes (= 90 minutes)"),
    ],
)
def test_dates_and_times(program, expected):
    assert ok_text(program) == expected


def test_adding_hours_to_a_plain_date_explains_itself():
    r = run("today() + hours(5)")
    assert r.category == "invalid"
    assert "no time of day" in r.text and "now()" in r.text
    assert run("today() + days(2)").ok  # whole days are fine


def test_elapsed_time_across_daylight_saving_change():
    # 2026-11-01 02:00 CDT -> 01:00 CST: one extra hour that night.
    span = "datetime(2026, 11, 1, 9, 0) - datetime(2026, 10, 31, 21, 0)"
    assert ok_text(span, zone=CHICAGO) == "result: 13 hours"
    assert ok_text(span) == "result: 12 hours"  # no zone: clock time
    assert ok_text("datetime(2026, 10, 31, 21, 0) + hours(12)", zone=CHICAGO) == (
        "result: 2026-11-01 08:00 (8:00 AM, Sunday)"
    )
    assert ok_text("hours(12) + datetime(2026, 10, 31, 21, 0)", zone=CHICAGO).startswith("result: 2026-11-01 08:00")
    # Same-evening leave-by math doesn't change.
    assert ok_text("at(18, 15) - minutes(25)", zone=CHICAGO) == "result: 2026-10-25 17:50 (5:50 PM, Sunday)"
    assert ok_text("date(2026, 11, 2) - date(2026, 10, 31)", zone=CHICAGO) == "result: 2 days"


@pytest.mark.parametrize(
    "program, with_zone, clock_time",
    [
        # spring forward, 2026-03-08 02:00 CST -> 03:00 CDT: one hour fewer that night
        ("datetime(2026, 3, 8, 9, 0) - datetime(2026, 3, 7, 21, 0)", "result: 11 hours", "result: 12 hours"),
        # a plain date is midnight, and then elapsed time applies ("hours until Monday")
        (
            "date(2026, 11, 2) - datetime(2026, 10, 31, 21, 0)",
            "result: 1 day 4 hours (= 28 hours)",
            "result: 1 day 3 hours (= 27 hours)",
        ),
        # leave-by math backwards across the change
        (
            "datetime(2026, 11, 1, 8, 0) - hours(12)",
            "result: 2026-10-31 21:00 (9:00 PM, Saturday)",
            "result: 2026-10-31 20:00 (8:00 PM, Saturday)",
        ),
        # += and sum() go through the same elapsed-time arithmetic as +
        (
            "t = datetime(2026, 10, 31, 21, 0)\nt += hours(12)",
            "result: 2026-11-01 08:00 (8:00 AM, Sunday)",
            "result: 2026-11-01 09:00 (9:00 AM, Sunday)",
        ),
        (
            "sum([datetime(2026, 10, 31, 21, 0), hours(12)])",
            "result: 2026-11-01 08:00 (8:00 AM, Sunday)",
            "result: 2026-11-01 09:00 (9:00 AM, Sunday)",
        ),
    ],
)
def test_every_date_time_path_uses_elapsed_time_with_a_zone(program, with_zone, clock_time):
    assert ok_text(program, zone=CHICAGO).splitlines()[0] == with_zone
    assert ok_text(program).splitlines()[0] == clock_time


def test_clock_and_zone_are_reset_after_every_run():
    run("today()", zone=CHICAGO)
    assert calculator._CLOCK.get() == datetime.now  # == : bound methods are rebuilt on access
    assert calculator._ZONE.get() is None
    run("1 / 0", zone=CHICAGO)
    assert calculator._CLOCK.get() == datetime.now
    assert calculator._ZONE.get() is None


# --------------------------------------------------------------------------- refused constructs


@pytest.mark.parametrize(
    "program, category",
    [
        ("import os", "not_allowed"),
        ("from math import sqrt", "not_allowed"),
        ("__import__('os')", "invalid"),  # no such function
        ("().__class__", "not_allowed"),
        ("(1).real", "not_allowed"),
        ("x = [1, 2]\nx[0]", "not_allowed"),
        ("'a' * 10", "not_allowed"),
        ("b'x'", "not_allowed"),
        ("f'{1}'", "not_allowed"),
        ("None", "not_allowed"),
        ("...", "not_allowed"),
        ("1j", "not_allowed"),
        ("[1] * 10**9", "invalid"),
        ("[1, 2] + [3]", "invalid"),
        ("for i in [1, 2]: pass", "not_allowed"),
        ("while 1: pass", "not_allowed"),
        ("def f(): pass", "not_allowed"),
        ("lambda: 1", "not_allowed"),
        ("class A: pass", "not_allowed"),
        ("[i for i in [1, 2]]", "not_allowed"),
        ("(x := 3)", "not_allowed"),
        ("x = 1\nif x: x = 2", "not_allowed"),
        ("del x", "not_allowed"),
        ("global x", "not_allowed"),
        ("assert 1", "not_allowed"),
        ("raise ValueError", "not_allowed"),
        ("try:\n1\nexcept Exception:\n2", "invalid"),  # lines are stripped, so no block parses
        ("x: int = 1", "not_allowed"),
        ("a, b = 1, 2", "not_allowed"),
        ("a = b = 5", "not_allowed"),
        ("_x = 1", "not_allowed"),
        ("dоse = 5", "not_allowed"),  # Cyrillic о: names are plain ASCII
        ("丁 = 1", "not_allowed"),
        ("a" * 33 + " = 1", "not_allowed"),
        ("sqrt", "invalid"),
        ("foo(1)", "invalid"),
        ("math.sqrt(4)", "not_allowed"),
        ("sum(*[1, 2])", "not_allowed"),
        ("max(**{})", "not_allowed"),
        ("round(1.5, digits=1)", "invalid"),
        ("1 << 2", "not_allowed"),
        ("~1", "not_allowed"),
        ("1 in [1]", "not_allowed"),
        ("1 is 1", "not_allowed"),
        ("{1: 2}", "not_allowed"),
        ("{1, 2}", "not_allowed"),
        ("[[1, 2]]", "not_allowed"),
        ("0 if 1 else __import__('os')", "invalid"),  # checked even on the branch that wouldn't run
        ("3 > 4 is 1", "not_allowed"),
    ],
)
def test_constructs_outside_the_language_are_refused(program, category):
    r = run(program)
    assert not r.ok
    assert r.category == category, r.text
    assert r.text.startswith("calculate error")


@pytest.mark.parametrize(
    "program, hint",
    [
        ("1,250 * 2", "not 1,250"),
        ("total = 1,250", "not 1,250"),
        ("2 ^ 10", "use **"),
        ("20%", "no $, commas or %"),
        ("$84.50 * 2", "no $, commas or %"),
    ],
)
def test_common_slips_get_a_hint(program, hint):
    r = run(program)
    assert not r.ok
    assert hint in r.text


@pytest.mark.parametrize(
    "program, category",
    [
        ("1/0", "math_error"),
        ("5 % 0", "math_error"),
        ("sqrt(-1)", "math_error"),
        ("log(0)", "math_error"),
        ("(-8) ** (1 / 3)", "math_error"),
        ("1e999", "too_large"),
        ("1e308 * 10", "too_large"),
        ("date(2026, 2, 30)", "math_error"),
        ("at(25, 0)", "math_error"),
        ("factorial(-1)", "math_error"),
        ("exp(1000)", "too_large"),
        ("days(10**12)", "too_large"),
        ("x += 1", "invalid"),
        ("unknown + 1", "invalid"),
        ("sqrt(1, 2)", "invalid"),
        ("min(today(), 3)", "invalid"),
        ("in_days(5)", "invalid"),
        ("mean([today()])", "invalid"),
        ("today() * 2", "invalid"),
        ("factorial(2.5)", "invalid"),
    ],
)
def test_runtime_errors_are_reported_not_raised(program, category):
    r = run(program)
    assert not r.ok
    assert r.category == category, r.text
    assert r.text.startswith("calculate error (line 1):")


def test_error_names_the_line_even_after_blank_lines():
    assert run("a = 1\nb = 2\na / 0").text == "calculate error (line 3): division by zero"
    assert run("\n\n\nx = 1 / 0").text == "calculate error (line 4): division by zero"
    assert run("a = 1\nb = (\n").text.startswith("calculate error (line 2)")


@pytest.mark.parametrize(
    "program, expected",
    [
        # refused up front by _validate: the line comes from the offending node
        ("a = 1\nb = 2\nimport os", "calculate error (line 3): imports are not available"),
        ("a = 1\n\nb = 'x'", "calculate error (line 3): text strings are not available"),
        ("a = 1\nb = foo(2)", "calculate error (line 2): there is no function 'foo'"),
        ("a = 1\nb = [" + ", ".join(["1"] * 201) + "]", "calculate error (line 2): list longer than 200"),
        # raised while running: the line comes from the statement being run
        ("a = 1\nunknown + 1", "calculate error (line 2): unknown name 'unknown'"),
        ("a = 1\nb = sqrt(1, 2)", "calculate error (line 2): sqrt() was called with the wrong number"),
        ("a = 1\nb = 2 ** 10**9", "calculate error (line 2): number too large"),
    ],
)
def test_refusals_and_calc_errors_name_their_line(program, expected):
    assert run(program).text.startswith(expected)


_ATOMS = ["0", "1", "-3", "2.5", "1e308", "2**4000", "True", "pi", "today()", "now()", "hours(2)", "minutes(-30)"] + [
    "days(1.5)",
    "date(2026, 3, 8)",
    "datetime(2026, 11, 1, 1, 30)",
    "date(9999, 12, 31)",
    "date(1, 1, 1)",
]
_UNARY = ["abs", "round", "int", "floor", "sqrt", "exp", "ln", "asin", "factorial", "days", "hours", "in_days"]
_NARY = ["min", "max", "sum", "mean", "median", "gcd", "lcm", "hypot", "pow", "comb", "perm", "log", "round"]
_OPS = ["+", "-", "*", "/", "//", "%", "**", "<", "==", "and", "or"]


def _random_expr(rng: random.Random, depth: int, names: list[str]) -> str:
    r = rng.random()
    if depth <= 0 or r < 0.3:
        return rng.choice(_ATOMS + names)
    if r < 0.55:
        return f"({_random_expr(rng, depth - 1, names)} {rng.choice(_OPS)} {_random_expr(rng, depth - 1, names)})"
    if r < 0.7:
        return f"{rng.choice(_UNARY)}({_random_expr(rng, depth - 1, names)})"
    if r < 0.85:
        args = ", ".join(_random_expr(rng, depth - 1, names) for _ in range(rng.randint(1, 3)))
        return f"{rng.choice(_NARY)}({args})"
    if r < 0.93:
        return "[" + ", ".join(_random_expr(rng, depth - 1, names) for _ in range(rng.randint(0, 3))) + "]"
    a, b, c = (_random_expr(rng, depth - 1, names) for _ in range(3))
    return f"({a} if {b} else {c})"


def test_evaluate_never_raises_on_random_programs():
    # The contract the handler relies on: every outcome is a CalcResult with a
    # category from CATEGORIES (the metric's label set), ok iff "ok", within
    # the output budget, and the clock/zone are put back. Seeded, so a failure
    # reproduces; the program is in the assertion message.
    rng = random.Random(20261025)
    for i in range(1500):
        names: list[str] = []
        lines = []
        for j in range(rng.randint(1, 5)):
            if rng.random() < 0.6:
                lines.append(f"v{j} = {_random_expr(rng, 3, names)}")
                names.append(f"v{j}")
            else:
                lines.append(_random_expr(rng, 3, names))
        program = "\n".join(lines)
        r = run(program, zone=CHICAGO if i % 2 else None)
        assert r.category in CATEGORIES, program
        assert r.ok == (r.category == "ok"), program
        assert r.text and len(r.text) <= calculator.MAX_OUTPUT_CHARS + 100, program
        assert "could not run this program" not in r.text, program  # no error escaped to the catch-all
    assert calculator._CLOCK.get() == datetime.now
    assert calculator._ZONE.get() is None


@pytest.mark.parametrize("program", ["", "   \n  ", "# 18% of 64 is 11.52", "\n\n# note\n"])
def test_empty_programs(program):
    r = run(program)
    assert not r.ok
    assert r.category == "empty"


def test_syntax_errors():
    for program in ("1 +", "x = = 1", "1\x00"):
        r = run(program)
        assert r.category == "invalid", program
        assert "Write plain numbers" in r.text


# --------------------------------------------------------------------------- size caps


@pytest.mark.parametrize(
    "program, category",
    [
        ("2 ** 10**9", "too_large"),
        ("10 ** 5000", "too_large"),
        ("x = 10 ** 1000\nx * x * x * x * x", "too_large"),
        ("factorial(100000)", "too_large"),
        ("comb(10**6, 500000)", "too_large"),
        ("perm(10**6)", "too_large"),
        ("round(1, -10**9)", "too_large"),
        ("round(1.5, 10**9)", "too_large"),
        ("lcm(" + ", ".join(str(p) for p in range(10**6, 10**6 + 25)) + ")", "too_large"),  # argument count
        ("lcm(2 ** 4000 + 1, 2 ** 4000 + 3)", "too_large"),  # two arguments, result past MAX_INT_BITS
        ("[" + ", ".join(["1"] * 201) + "]", "too_large"),
        ("x = 1\n" * 41, "too_large"),
        ("1 + " * 600 + "1", "too_large"),
        ("+".join(["1"] * 250), "not_allowed"),
        ("-" * 1990 + "1", "too_large"),
        ("(" * 300 + "1" + ")" * 300, "invalid"),
        ("1" * (calculator.MAX_PROGRAM_CHARS + 1), "too_large"),
    ],
)
def test_size_caps_refuse_quickly(program, category):
    t0 = time.perf_counter()
    r = run(program)
    assert time.perf_counter() - t0 < 1.0
    assert not r.ok
    assert r.category == category, r.text


_ONES = calculator.MAX_LIST_LEN * ["1"]
_SIXES = calculator.MAX_GCD_ARGS * ["6"]


@pytest.mark.parametrize(
    "at_cap, one_over",
    [
        ("1 + 1" + " " * (calculator.MAX_PROGRAM_CHARS - 5), "1 + 1" + " " * (calculator.MAX_PROGRAM_CHARS - 4)),
        ("x = 1\n" * calculator.MAX_STATEMENTS, "x = 1\n" * (calculator.MAX_STATEMENTS + 1)),
        ("sum([" + ", ".join(_ONES) + "])", "sum([" + ", ".join(_ONES + ["1"]) + "])"),
        ("gcd(" + ", ".join(_SIXES) + ")", "gcd(" + ", ".join(_SIXES + ["6"]) + ")"),
        (f"round(1.5, {calculator.MAX_ROUND_DIGITS})", f"round(1.5, {calculator.MAX_ROUND_DIGITS + 1})"),
        (f"round(1.5, -{calculator.MAX_ROUND_DIGITS})", f"round(1.5, -{calculator.MAX_ROUND_DIGITS + 1})"),
    ],
)
def test_caps_allow_exactly_the_limit(at_cap, one_over):
    assert run(at_cap).ok, run(at_cap).text
    assert run(one_over).category == "too_large"


def test_chained_alias_amplification_is_refused():
    # Security review: one Assign with ~800 targets made the report build
    # ~196M chars (2.8 s, 770 MB) before the output cap applied.
    program = (
        "a = 2 ** 4095 - 0\nb = [" + ", ".join(["a"] * 200) + "]\n" + "=".join(f"v{i}" for i in range(100)) + " = b"
    )
    assert len(program) <= calculator.MAX_PROGRAM_CHARS
    t0 = time.perf_counter()
    r = run(program)
    assert time.perf_counter() - t0 < 0.2
    assert r.category == "not_allowed"


def test_worst_case_report_stays_within_budget():
    lines = ["a = 2 ** 4095 - 1", "b = [" + ", ".join(["a"] * 200) + "]"]
    lines += [f"v{i} = b" for i in range(38)]
    program = "\n".join(lines)
    assert len(program) <= calculator.MAX_PROGRAM_CHARS
    t0 = time.perf_counter()
    r = run(program)
    assert time.perf_counter() - t0 < 0.2
    assert r.ok
    assert len(r.text) <= calculator.MAX_OUTPUT_CHARS + 100
    assert "…" in r.text


def test_output_never_cuts_a_number_mid_way():
    r = run("\n".join(f"v{i} = 2 ** 4000" for i in range(40)))
    assert r.ok
    for line in r.text.splitlines():
        assert line.endswith("digits)") or line.startswith("…"), line


# --------------------------------------------------------------------------- medication refusal


@pytest.mark.parametrize(
    "program",
    [
        "dose = 50\ndose * 2",
        "dailyDose = 40",
        "DoSE = 50",
        "DOSE = 50",
        "dose_mg = 5",
        "overdose_risk = 1",
        "pills_left = 30\npills_left / 2",
        "next_dose_time = at(14, 0) + hours(4)",
        "medication_total = 3 * 20",
        # checked before anything runs: not a math_error, and a branch that
        # wouldn't run still counts
        "dose = 1 / 0",
        "x = 2 if 1 else dose",
        "x = round(3.14159, ndigits=pills)",
    ],
)
def test_dose_words_are_refused(program):
    r = run(program)
    assert r.category == "refused_dose_word", r.text
    assert not r.ok


def test_users_medication_names_are_refused():
    med_tokens = medication_name_tokens({"daily": {"morning": [{"name": "Exampleamine XR"}]}})
    for program in ("exampleamine_total = 20 * 2", "ExampleamineStrength = 20", "EXAMPLEAMINE = 1"):
        assert run(program, med_tokens=med_tokens).category == "refused_med_name", program
    # "XR" is too short to be a name token, so it doesn't block unrelated math.
    assert run("xr = 2 + 2", med_tokens=med_tokens).ok


def test_refusal_does_not_ask_the_model_to_recite_medications():
    for r in (run("dose = 5"), run("zz = 1", med_tokens=frozenset({"zz"}))):
        assert "prescriber" in r.text
        assert "MEDICATIONS" not in r.text
        assert r.text == calculator._REFUSAL_TEXT  # identical: never says which word matched


@pytest.mark.parametrize(
    "program",
    [
        "med = median([1, 2, 3])",
        "medium_pizza = 14.99 * 2",
        "tablet_price = 329 * 1.08",
        "coffee_capsules = 30 * 0.6",
        "sodium_mg = 450 * 2",
        "caffeine_mg = 95 * 3",
        "rx = 3\nry = 4\nhypot(rx, ry)",
    ],
)
def test_ordinary_names_are_not_refused(program):
    assert run(program).ok, program


def test_everyday_words_in_medication_names_are_ignored():
    meds = {
        "daily": {"evening": [{"name": "Night Time Sleep Aid"}, {"name": "Extra Strength Painaway"}]},
        "weekly": [{"name": "Fish Oil"}, {"name": "Vitamin D3"}, {"name": "Claritin 24 Hour"}],
    }
    tokens = medication_name_tokens(meds)
    assert tokens == frozenset({"painaway", "claritin"})
    for program in ("leave_time = at(18, 15) - minutes(25)", "extra = 5", "fish = 2", "hour = 18"):
        assert run(program, med_tokens=tokens).ok, program


def test_medication_name_tokens_reads_every_bucket():
    meds = {
        "daily": {"morning": [{"name": "Alphazine"}], "evening": [{"name": "Beta Tonic"}]},
        "weekly": [{"name": "Gammacil"}],
        "as_needed": [{"name": "Delta"}, {"no_name": True}, "junk"],
    }
    assert medication_name_tokens(meds) == frozenset({"alphazine", "beta", "tonic", "gammacil", "delta"})
    assert medication_name_tokens({}) == frozenset()
    assert medication_name_tokens(None) == frozenset()
    assert medication_name_tokens({"daily": "bad", "weekly": "bad"}) == frozenset()


# --------------------------------------------------------------------------- handler, metric, schema, prompt


def _count(result: str) -> float:
    return CALCULATE_TOTAL.labels(result=result)._value.get()


def test_metric_labels_match_categories():
    assert {key[0] for key in CALCULATE_TOTAL._metrics} == set(CATEGORIES)


@pytest.mark.asyncio
async def test_handler_returns_text_and_counts(monkeypatch):
    monkeypatch.setattr(tool_handlers, "get_medications", lambda: {})
    before = _count("ok")
    out = await execute_tool("calculate", {"program": "(84.50 * 1.2) / 3"})
    assert out == "result: 33.8"
    assert _count("ok") == before + 1


@pytest.mark.asyncio
async def test_handler_accepts_expression_alias_and_line_lists(monkeypatch):
    monkeypatch.setattr(tool_handlers, "get_medications", lambda: {})
    assert await execute_tool("calculate", {"expression": "2 + 2"}) == "result: 4"
    out = await execute_tool("calculate", {"program": ["a = 2", "a * 3"]})
    assert out.splitlines()[0] == "result: 6"


@pytest.mark.asyncio
async def test_handler_uses_the_local_zone(monkeypatch):
    monkeypatch.setattr(tool_handlers, "get_medications", lambda: {})
    monkeypatch.setattr(tool_handlers.shared, "TIMEZONE", "America/Chicago")
    out = await execute_tool("calculate", {"program": "datetime(2026, 11, 1, 9, 0) - datetime(2026, 10, 31, 21, 0)"})
    assert out == "result: 13 hours"
    monkeypatch.setattr(tool_handlers.shared, "TIMEZONE", "Not/AZone")
    out = await execute_tool("calculate", {"program": "datetime(2026, 11, 1, 9, 0) - datetime(2026, 10, 31, 21, 0)"})
    assert out == "result: 12 hours"  # bad TZ setting: clock-time fallback, no crash


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", ["", "../etc/localtime"])
async def test_handler_falls_back_on_a_malformed_zone_key(monkeypatch, tz):
    # ZoneInfo raises ValueError (not ZoneInfoNotFoundError/KeyError) for these.
    monkeypatch.setattr(tool_handlers, "get_medications", lambda: {})
    monkeypatch.setattr(tool_handlers.shared, "TIMEZONE", tz)
    before = _count("ok")
    out = await execute_tool("calculate", {"program": "datetime(2026, 11, 1, 9, 0) - datetime(2026, 10, 31, 21, 0)"})
    assert out == "result: 12 hours"
    assert _count("ok") == before + 1


@pytest.mark.asyncio
async def test_handler_refuses_the_users_medication_names(monkeypatch):
    monkeypatch.setattr(tool_handlers, "get_medications", lambda: {"as_needed": [{"name": "Exampleamine"}]})
    before = _count("refused_med_name")
    out = await execute_tool("calculate", {"program": "exampleamine_each = 30 / 2"})
    assert out.startswith("calculate refused")
    assert _count("refused_med_name") == before + 1


@pytest.mark.asyncio
async def test_handler_reports_errors_without_raising(monkeypatch):
    monkeypatch.setattr(tool_handlers, "get_medications", lambda: {})
    before = _count("not_allowed")
    out = await execute_tool("calculate", {"program": "__import__('os').system('id')"})
    assert out.startswith("calculate error")
    assert _count("not_allowed") == before + 1
    before = _count("empty")
    assert (await execute_tool("calculate", {})).startswith("calculate error")
    assert (await execute_tool("calculate", {"program": "# just a note"})).startswith("calculate error")
    assert _count("empty") == before + 2


@pytest.mark.asyncio
async def test_handler_logs_no_program_content(monkeypatch, caplog):
    monkeypatch.setattr(tool_handlers, "get_medications", lambda: {"as_needed": [{"name": "Exampleamine"}]})
    caplog.set_level(logging.DEBUG)
    await execute_tool("calculate", {"program": "exampleamine_each = 31.5 / 2"})
    await execute_tool("calculate", {"program": "secret_total = 4417.25 +"})
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "[CALCULATE] refused_med_name" in logged
    assert "[CALCULATE] invalid" in logged
    for fragment in ("exampleamine", "Exampleamine", "31.5", "secret_total", "4417"):
        assert fragment not in logged


def _names(tools):
    return {t.get("function", {}).get("name") for t in tools}


def test_schema_is_in_chat_and_voice_tools():
    tool_definitions._tools_cache_key = ()
    try:
        assert "calculate" in _names(tool_definitions.get_all_tools())
        assert "calculate" in _names(tool_definitions.get_voice_tools())
    finally:
        tool_definitions._tools_cache_key = ()


def test_schema_lists_only_real_functions():
    schema = next(t for t in tool_definitions.STATIC_TOOLS if t["function"]["name"] == "calculate")
    description = schema["function"]["description"]
    for name in calculator.FUNCTIONS:
        assert name in description, name


def test_schema_calls_no_function_the_calculator_lacks():
    # The other direction: every name(...) the description shows the model is real.
    schema = next(t for t in tool_definitions.STATIC_TOOLS if t["function"]["name"] == "calculate")
    called = set(re.findall(r"\b([a-z_][a-z0-9_]*)\(", schema["function"]["description"]))
    assert {"at", "datetime", "log", "today"} <= called  # the scan finds the call examples
    assert called <= set(calculator.FUNCTIONS), called - set(calculator.FUNCTIONS)


def test_calculate_is_not_terminal():
    assert "calculate" not in TERMINAL_TOOLS


@pytest.mark.parametrize("is_voice", [False, True])
def test_math_rule_is_in_the_static_prompt(is_voice):
    prompt = prompt_builder.get_unified_system_prompt(is_voice=is_voice)
    static = prompt.split(prompt_builder.DYNAMIC_CONTEXT_MARKER, 1)[0]
    assert "- MATH:" in static
    assert "call calculate" in static
    assert "Never use calculate for medication doses" in static


# --------------------------------------------------------------------------- review follow-ups


def test_only_real_line_breaks_split_the_program():
    # Python reads a form feed as plain whitespace inside a line.
    assert ok_text("1\f+ 1") == "result: 2"
    assert ok_text("a = 1\r\nb = 2\ra + b").splitlines()[0] == "result: 3"


def test_mean_of_durations():
    assert ok_text("mean([minutes(25), minutes(31), minutes(40)])") == "result: 32 minutes"
    assert run("mean([minutes(5), 3])").category == "invalid"


def test_accented_medication_names_are_folded():
    tokens = medication_name_tokens({"as_needed": [{"name": "M\u00e9thylph\u00e9nidate"}]})
    assert tokens == frozenset({"methylphenidate"})
    assert run("methylphenidate_each = 10 / 2", med_tokens=tokens).category == "refused_med_name"
