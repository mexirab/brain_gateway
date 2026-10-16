"""travel_time.spoken_location / is_virtual_location — what gets read aloud."""

import pytest

from orchestrator.travel_time import is_virtual_location, spoken_location


@pytest.mark.parametrize(
    "loc",
    [
        "https://example.webex.com/example/j.php?MTID=m0123",
        "http://example.test/room",
        "www.example.test/join",
        "Microsoft Teams Meeting",
        "Teams meeting",
        "Zoom",
        "https://us02web.zoom.us/j/123",
        "meet.google.com/abc-defg-hij",
        "Google Meet",
        "Webex Personal Room",
        "GoToMeeting",
        "GoTo Meeting",
        "Amazon Chime",
        "Teams",
        "Microsoft Teams",
    ],
)
def test_virtual_locations_are_never_spoken(loc):
    assert is_virtual_location(loc)
    assert spoken_location(loc) == ""


@pytest.mark.parametrize(
    ("loc", "spoken"),
    [
        ("2 Example Ave", "2 Example Ave"),
        ("Conference Room 4", "Conference Room 4"),
        ("  Dentist office,\n  Main St ", "Dentist office, Main St"),
        ("Steamboat Springs", "Steamboat Springs"),
        ("Teamster Hall", "Teamster Hall"),
        ("Teams Cafe, 5 Example Rd", "Teams Cafe, 5 Example Rd"),
    ],
)
def test_physical_locations_are_spoken_on_one_line(loc, spoken):
    assert not is_virtual_location(loc)
    assert spoken_location(loc) == spoken


def test_empty_and_overlong_are_not_spoken():
    assert spoken_location("") == ""
    assert spoken_location(None) == ""  # type: ignore[arg-type]
    assert spoken_location("Building " + "A" * 200) == ""


def test_known_false_positive_is_documented():
    """A real place named "Zoom ..." is treated as a meeting: not spoken, not
    sent to Maps. Accepted trade-off — pinned so a change is deliberate."""
    assert spoken_location("Zoom Room 3, Building 2") == ""


# -- the 5-minute prep hint ---------------------------------------------------


class _E:
    def __init__(self, location="", description=""):
        self.location = location
        self.description = description


@pytest.mark.parametrize(
    ("location", "description", "hint"),
    [
        ("https://example.webex.com/j.php?MTID=m1", "", "Pull up the meeting link."),
        ("Microsoft Teams Meeting", "", "Pull up the meeting link."),
        ("", "join at https://zoom.us/j/1", "Pull up the meeting link."),
        ("2 Example Ave", "", "Head out to 2 Example Ave."),
        ("Building " + "A" * 200, "agenda attached", "Check the agenda."),
    ],
)
def test_prep_hint(location, description, hint):
    from orchestrator.jobs_calendar import _get_prep_hint

    assert _get_prep_hint(_E(location, description)) == hint
