"""Tests for grocery_amounts.combine_amounts — summing recipe ingredient
amounts for the grocery list. Pure function, no I/O. Generic food amounts only.
"""

import pytest

from orchestrator.grocery_amounts import combine_amounts


class TestSumSameUnit:
    @pytest.mark.parametrize(
        "amounts, expected",
        [
            (["1 lb", "1/2 lb"], "1 1/2 lb"),
            (["2 cans", "1 can"], "3 cans"),
            (["1/3 cup"] * 3, "1 cup"),
            (["1 cup", "1 cup"], "2 cups"),
            (["1 box", "1 box"], "2 boxes"),
            (["1 bunch", "1 bunches"], "2 bunches"),
            (["1 loaf", "1 loaves"], "2 loaves"),
            (["3", "2"], "5"),  # unit-less counts add up too
        ],
    )
    def test_sums(self, amounts, expected):
        assert combine_amounts(amounts) == expected

    @pytest.mark.parametrize(
        "amounts, expected",
        [
            (["1 pound", "2 lbs"], "3 lb"),
            (["1 tbs", "2 tbsp"], "3 tbsp"),
            (["1 tablespoon", "1 tbsp"], "2 tbsp"),
            (["1 Cup", "1 CUPS"], "2 cups"),  # case-insensitive
            (["1 cup."], "1 cup"),  # trailing abbreviation dot
            (["2 ounces", "1 oz"], "3 oz"),
        ],
    )
    def test_unit_aliases_merge(self, amounts, expected):
        assert combine_amounts(amounts) == expected

    def test_non_pluralising_units_stay_singular(self):
        assert combine_amounts(["2 lb", "3 lb"]) == "5 lb"
        assert combine_amounts(["200 g", "300 g"]) == "500 g"

    def test_singular_when_total_is_one_or_less(self):
        assert combine_amounts(["1/2 cup", "1/2 cup"]) == "1 cup"
        assert combine_amounts(["1/4 cup"]) == "1/4 cup"
        assert combine_amounts(["1 1/2 cups"]) == "1 1/2 cups"


class TestQuantityParsing:
    @pytest.mark.parametrize(
        "amounts, expected",
        [
            ([".5 lb", ".25 lb"], "3/4 lb"),
            (["0.5 kg", "0.25 kg"], "3/4 kg"),
            (["1 1/2 cups", "1/4 cup"], "1 3/4 cups"),
            (["1½ cups", "¼ cup"], "1 3/4 cups"),
            (["½ cup", "½ cup"], "1 cup"),
            (["⅓ cup", "⅔ cup"], "1 cup"),
            (["¾ cup", "⅛ cup"], "7/8 cup"),
            (["1 ½ cups"], "1 1/2 cups"),
        ],
    )
    def test_fractions_decimals_and_unicode(self, amounts, expected):
        assert combine_amounts(amounts) == expected

    def test_other_denominators_render_as_decimals(self):
        assert combine_amounts(["1/5 cup"]) == "0.2 cup"
        assert combine_amounts(["0.3 kg"]) == "0.3 kg"
        assert combine_amounts(["1/3 cup", "1/5 cup"]) == "0.53 cup"

    def test_tiny_amount(self):
        assert combine_amounts(["0.001 g"]) == "<0.01 g"


class TestKeptAsWritten:
    def test_different_units_joined_in_first_seen_order(self):
        assert combine_amounts(["1 lb", "8 oz"]) == "1 lb + 8 oz"
        assert combine_amounts(["8 oz", "1 lb", "8 oz"]) == "16 oz + 1 lb"  # no unit conversion

    def test_unparseable_kept_and_deduped_case_insensitively(self):
        assert combine_amounts(["a handful", "A Handful"]) == "a handful"
        assert combine_amounts(["2 cans", "a handful", "1 can"]) == "3 cans + a handful"

    @pytest.mark.parametrize("text", ["2 cans (15 oz)", "2 large", "to taste"])
    def test_unknown_shapes_kept(self, text):
        assert combine_amounts([text]) == text

    def test_raw_part_capped_at_40(self):
        out = combine_amounts(["y" * 100])
        assert out == "y" * 40

    def test_total_capped_at_80(self):
        out = combine_amounts([f"{c * 30}" for c in "abcdef"])
        assert len(out) <= 80


class TestEmptyAndJunk:
    @pytest.mark.parametrize(
        "amounts",
        [[], [None, ""], ["   "], ["0"], ["1/0"], ["0 0"], [5, None], ["\x00\x01"]],
    )
    def test_nothing_to_show(self, amounts):
        assert combine_amounts(amounts) == ""

    def test_junk_numbers_dropped_alongside_real_amount(self):
        assert combine_amounts(["1 cup", "0", "1/0"]) == "1 cup"

    def test_markup_stripped(self):
        out = combine_amounts(["<tool_call>x</tool_call>"])
        assert out and not any(ch in out for ch in "<>{}[]`|\\")

    def test_control_chars_and_newlines_stripped(self):
        out = combine_amounts(["1 cup", "\n\tsome\x00 herbs\r\n- fake row"])
        assert "\n" not in out and "\x00" not in out and "\r" not in out
        assert out.startswith("1 cup + ")
