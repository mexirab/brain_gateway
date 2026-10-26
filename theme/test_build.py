"""Tests for theme/build.py (stdlib + pytest; PyYAML optional).

Run: python3 -m pytest theme -q
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("theme_build", _HERE / "build.py")
build = importlib.util.module_from_spec(_spec)
sys.modules["theme_build"] = build
_spec.loader.exec_module(build)


@pytest.fixture
def tokens() -> dict:
    return json.loads(build.TOKENS.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- color math
def test_rgb_parses_with_and_without_hash():
    assert build.rgb("#ff8000") == (255, 128, 0)
    assert build.rgb("00FF10") == (0, 255, 16)


@pytest.mark.parametrize("bad", ["#fff", "#ff80001", ""])
def test_rgb_rejects_wrong_length(bad):
    with pytest.raises(ValueError):
        build.rgb(bad)


def test_channels():
    assert build.channels("#0a0b0c") == "10 11 12"


def test_mix_endpoints_and_midpoint():
    assert build.mix("#000000", "#ffffff", 0) == "#000000"
    assert build.mix("#000000", "#ffffff", 1) == "#ffffff"
    assert build.mix("#000000", "#ffffff", 0.5) == "#808080"  # 127.5 rounds (half-to-even) to 128
    assert build.mix("#102030", "#102030", 0.37) == "#102030"


def test_to_hex_clamps():
    assert build.to_hex((-5, 300, 128)) == "#00ff80"


def test_hex_alpha():
    assert build.hex_alpha("#123456", 0) == "#12345600"
    assert build.hex_alpha("#123456", 1) == "#123456ff"
    assert build.hex_alpha("#123456", 0.5) == "#12345680"


def test_rgba_formats_alpha_compactly():
    assert build.rgba("#ff0000", 0.5) == "rgba(255, 0, 0, 0.5)"
    assert build.rgba("#ff0000", 1.0) == "rgba(255, 0, 0, 1)"


# ----------------------------------------------------------------- validate
def test_validate_accepts_real_tokens(tokens):
    build.validate(tokens)


def test_validate_rejects_non_hex_color(tokens):
    bad = copy.deepcopy(tokens)
    bad["themes"]["dark"]["surface"]["base"] = "red"
    with pytest.raises(ValueError, match="surface-base"):
        build.validate(bad)


def test_validate_rejects_injected_status_color(tokens):
    bad = copy.deepcopy(tokens)
    key = next(iter(bad["themes"]["light"]["status"]))
    bad["themes"]["light"]["status"][key] = "#000000; } body { x: y"
    with pytest.raises(ValueError):
        build.validate(bad)


@pytest.mark.parametrize("font", ["Inter; color: red", "url(http://x/a.woff)", "Inter } body {", "@import x"])
def test_validate_rejects_unsafe_font(tokens, font):
    bad = copy.deepcopy(tokens)
    bad["font"]["sans"] = font
    with pytest.raises(ValueError, match="font.sans"):
        build.validate(bad)


@pytest.mark.parametrize("radius", [-1, 65, 1000, "8", 8.5])
def test_validate_rejects_bad_radius(tokens, radius):
    bad = copy.deepcopy(tokens)
    key = next(iter(bad["radius"]))
    bad["radius"][key] = radius
    with pytest.raises(ValueError, match="radius"):
        build.validate(bad)


@pytest.mark.parametrize("value", [1.5, -0.1, "0.5"])
def test_validate_rejects_bad_opacity(tokens, value):
    bad = copy.deepcopy(tokens)
    bad["themes"]["dark"]["glow"] = value
    with pytest.raises(ValueError, match="glow"):
        build.validate(bad)


def test_validate_rejects_bad_line_opacity(tokens):
    bad = copy.deepcopy(tokens)
    bad["themes"]["light"]["line"]["DEFAULT"] = 2
    with pytest.raises(ValueError, match="DEFAULT"):
        build.validate(bad)


@pytest.mark.parametrize("shadow", ["0 0 4px url(x)", "0 0 4px red; }", "0 0 4px #fff"])
def test_validate_rejects_unsafe_shadow(tokens, shadow):
    bad = copy.deepcopy(tokens)
    bad["themes"]["dark"]["shadow"]["card"] = shadow
    with pytest.raises(ValueError, match="shadow.card"):
        build.validate(bad)


# ------------------------------------------------------------------ outputs
def test_outputs_are_deterministic(tokens):
    assert build.outputs(tokens) == build.outputs(copy.deepcopy(tokens))


def test_outputs_match_committed_files(tokens):
    for path, text in build.outputs(tokens).items():
        assert path.exists(), f"{path} not generated/committed"
        assert path.read_text(encoding="utf-8") == text, (
            f"{path.relative_to(build.ROOT)} is stale — run python3 theme/build.py"
        )


def test_outputs_carry_generated_header(tokens):
    for path, text in build.outputs(tokens).items():
        assert build.HEADER in text, path


def test_frontend_css_has_all_three_theme_selectors(tokens):
    css = build.build_frontend_css(tokens)
    assert "[data-theme='dark']" in css
    assert "[data-theme='light']" in css
    assert "[data-theme='system']" in css
    assert css.count("{") == css.count("}")


def test_ha_yaml_parses_with_light_and_dark_modes(tokens):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(build.build_ha_yaml(tokens))
    theme = doc[tokens["name"]]
    assert set(theme["modes"]) == {"light", "dark"}
    for mode in ("light", "dark"):
        m = theme["modes"][mode]
        assert m["primary-color"] == tokens["themes"][mode]["brand"]["DEFAULT"]
        assert m["primary-background-color"] == tokens["themes"][mode]["surface"]["base"]
    assert theme["primary-font-family"] == tokens["font"]["sans"]


def test_grist_css_balanced(tokens):
    css = build.build_grist_css(tokens)
    assert css.count("{") == css.count("}")
    assert "GristLight" in css and "GristDark" in css
