"""Tests for diet_profiles (F-015 phase 2): validation, load/save round-trip,
audit row, rule/label helpers. Preference data is generic and invented."""

import json

import pytest


@pytest.fixture
def dp(tmp_db, tmp_path, monkeypatch):
    from orchestrator import diet_profiles, shared

    monkeypatch.setattr(shared, "DIET_PROFILE_PATH", str(tmp_path / "sub" / "diet_profile.yaml"))
    return diet_profiles


class TestValidate:
    def test_empty_gives_defaults(self, dp):
        assert dp.validate({}) == dp.DEFAULTS

    def test_defaults_not_shared_by_reference(self, dp):
        out = dp.validate({})
        out["reminders"]["prep"] = False
        out["avoid"].append("x")
        assert dp.DEFAULTS["reminders"]["prep"] is True
        assert dp.DEFAULTS["avoid"] == []

    def test_non_dict_rejected(self, dp):
        for bad in (None, [], "mediterranean", 3):
            with pytest.raises(ValueError):
                dp.validate(bad)

    @pytest.mark.parametrize("profile", ["keto", "", None, 1, ["vegan"], {"x": 1}])
    def test_bad_profile(self, dp, profile):
        with pytest.raises(ValueError, match="profile"):
            dp.validate({"profile": profile})

    def test_every_builtin_profile_accepted(self, dp):
        for key in dp.PROFILES:
            assert dp.validate({"profile": key})["profile"] == key

    @pytest.mark.parametrize(
        "key,bad",
        [
            ("servings", 0),
            ("servings", 9),
            ("servings", True),
            ("servings", 2.0),
            ("servings", "2"),
            ("batch_lunch_days", -1),
            ("batch_lunch_days", 7),
            ("batch_lunch_days", False),
            ("fresh_dinners_per_week", 8),
            ("max_dinner_minutes", 9),
            ("max_dinner_minutes", 181),
            ("max_dinner_minutes", None),
        ],
    )
    def test_int_ranges(self, dp, key, bad):
        with pytest.raises(ValueError, match=key):
            dp.validate({key: bad})

    @pytest.mark.parametrize(
        "key,good",
        [("servings", 8), ("batch_lunch_days", 0), ("fresh_dinners_per_week", 0), ("max_dinner_minutes", 10)],
    )
    def test_int_bounds_inclusive(self, dp, key, good):
        assert dp.validate({key: good})[key] == good

    @pytest.mark.parametrize("bad", ["monday", "", None, 0, ["sun"]])
    def test_bad_batch_day(self, dp, bad):
        with pytest.raises(ValueError, match="batch_day"):
            dp.validate({"batch_day": bad})

    def test_avoid_cleaned_and_blank_dropped(self, dp):
        out = dp.validate({"avoid": ["  peanuts ", "<script>shellfish</script>", "", None, "a\x00b", "x" * 100]})
        assert out["avoid"][0] == "peanuts"
        assert "<" not in out["avoid"][1] and "shellfish" in out["avoid"][1]
        assert "ab" in out["avoid"]
        assert len(out["avoid"][-1]) == 40
        assert len(out["avoid"]) == 4  # "" and None removed

    def test_avoid_and_rules_must_be_lists(self, dp):
        with pytest.raises(ValueError, match="avoid"):
            dp.validate({"avoid": "peanuts"})
        with pytest.raises(ValueError, match="extra_rules"):
            dp.validate({"extra_rules": {"a": 1}})

    def test_caps(self, dp):
        with pytest.raises(ValueError, match="avoid"):
            dp.validate({"avoid": [f"food{i}" for i in range(dp.MAX_AVOID + 1)]})
        with pytest.raises(ValueError, match="extra_rules"):
            dp.validate({"extra_rules": [f"rule {i}" for i in range(dp.MAX_EXTRA_RULES + 1)]})
        # Blank entries don't count against the cap.
        ok = dp.validate({"avoid": [f"food{i}" for i in range(dp.MAX_AVOID)] + ["", "  "]})
        assert len(ok["avoid"]) == dp.MAX_AVOID

    def test_extra_rule_length_cap(self, dp):
        assert len(dp.validate({"extra_rules": ["y" * 500]})["extra_rules"][0]) == 200

    def test_reminders_partial_merge(self, dp):
        out = dp.validate({"reminders": {"prep": False, "dinner_time": "18:05"}})
        assert out["reminders"] == {"prep": False, "dinners": True, "prep_time": "15:00", "dinner_time": "18:05"}

    @pytest.mark.parametrize(
        "rem",
        [
            "on",
            [],
            {"prep": "yes"},
            {"dinners": 1},
            {"prep_time": "3pm"},
            {"prep_time": "24:00"},
            {"dinner_time": "7:30"},
            {"dinner_time": "12:60"},
            {"dinner_time": 1730},
        ],
    )
    def test_bad_reminders(self, dp, rem):
        with pytest.raises(ValueError, match="reminders"):
            dp.validate({"reminders": rem})

    def test_unknown_keys_ignored(self, dp):
        assert "evil" not in dp.validate({"evil": "x"})


class TestLoadSave:
    def test_missing_file_gives_defaults(self, dp):
        assert dp.load() == dp.DEFAULTS

    def test_corrupt_yaml_gives_defaults(self, dp):
        import os

        os.makedirs(os.path.dirname(dp._path()), exist_ok=True)
        with open(dp._path(), "w") as f:
            f.write("profile: [unclosed\n  : :")
        assert dp.load() == dp.DEFAULTS

    def test_invalid_values_on_disk_give_defaults(self, dp):
        import os

        os.makedirs(os.path.dirname(dp._path()), exist_ok=True)
        with open(dp._path(), "w") as f:
            f.write("profile: keto\n")
        assert dp.load() == dp.DEFAULTS

    def test_non_dict_yaml_gives_defaults(self, dp):
        import os

        os.makedirs(os.path.dirname(dp._path()), exist_ok=True)
        with open(dp._path(), "w") as f:
            f.write("- a\n- b\n")
        assert dp.load() == dp.DEFAULTS

    def test_save_round_trip_and_partial_merge(self, dp):
        dp.save({"profile": "dash", "servings": 2, "avoid": ["peanuts"]})
        out = dp.save({"servings": 3})
        assert out["profile"] == "dash" and out["servings"] == 3 and out["avoid"] == ["peanuts"]
        assert dp.load() == out

    def test_save_nested_reminders_merge(self, dp):
        dp.save({"reminders": {"prep": False}})
        out = dp.save({"reminders": {"dinner_time": "19:00"}})
        assert out["reminders"]["prep"] is False
        assert out["reminders"]["dinner_time"] == "19:00"
        assert out["reminders"]["prep_time"] == "15:00"

    def test_save_rejects_non_dict(self, dp):
        with pytest.raises(ValueError):
            dp.save(["profile"])

    def test_invalid_save_leaves_file_untouched(self, dp):
        dp.save({"servings": 2})
        with pytest.raises(ValueError):
            dp.save({"servings": 99})
        assert dp.load()["servings"] == 2

    def test_save_writes_audit_row(self, dp):
        from orchestrator import state_store

        dp.save({"profile": "vegan"})
        with state_store.get_db() as conn:
            rows = conn.execute("SELECT diff_json FROM config_changes WHERE panel = 'diet'").fetchall()
        assert len(rows) == 1
        diff = json.loads(rows[0]["diff_json"])
        assert diff["before"]["profile"] == "mediterranean"
        assert diff["after"]["profile"] == "vegan"


class TestHelpers:
    def test_rules_for_profile_plus_extras(self, dp):
        rules = dp.rules_for({"profile": "vegan", "extra_rules": ["Spicy is fine"]})
        assert rules[: len(dp.PROFILES["vegan"]["rules"])] == dp.PROFILES["vegan"]["rules"]
        assert rules[-1] == "Spicy is fine"

    def test_rules_for_unknown_profile_falls_back_to_balanced(self, dp):
        assert dp.rules_for({"profile": "nope"}) == dp.PROFILES["balanced"]["rules"]
        assert dp.rules_for({}) == dp.PROFILES["balanced"]["rules"]

    def test_rules_for_does_not_mutate_builtin(self, dp):
        before = list(dp.PROFILES["dash"]["rules"])
        dp.rules_for({"profile": "dash", "extra_rules": ["x"]})
        assert dp.PROFILES["dash"]["rules"] == before

    def test_label_for(self, dp):
        assert dp.label_for("tlc") == "TLC (cholesterol-lowering)"
        assert dp.label_for("custom") == "custom"
        assert dp.label_for(None) == ""

    def test_public_profiles_shape(self, dp):
        pub = dp.public_profiles()
        assert {p["key"] for p in pub} == set(dp.PROFILES)
        assert all(set(p) == {"key", "label", "description"} for p in pub)
