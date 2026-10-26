"""Did the user actually ask for a device change? — a deterministic gate in
front of every ``home_assistant`` call the chat model makes.

The model decides when to call ``home_assistant``; ``ha_guard`` decides which
calls are allowed at all, but neither knew whether the user asked. On
2026-10-24 a trial brain turned off speakers or a light on 4 of 8 voice "tell
me a fun fact" turns with thinking off. Turning thinking on hid it (0/33, then
0/70), but that is model behaviour, not a control — and it does nothing when
a web page, email or memory talks the model into a device call. So
``unified_loop`` runs ``check_call`` before each one, and an unrequested call
is answered with ``UNREQUESTED_RESULT`` instead of executing.

What counts as asking, judged on the user's latest message (photo-analysis
text already stripped; the first ``_MAX_CHARS`` only):

- a control verb phrase — "turn/switch … on/off", "turn it up", dim,
  brighten, mute, pause, play, "set … to 70" (not a reminder/timer/alarm),
  "make them blue", lock (not "lock in" / "lock screen"), "close it";
- a device word (light, fan, speaker, music, TV, thermostat, heat, AC, blinds,
  volume, …) together with an action verb (stop the music, close the blinds),
  a cue right after it ("lights off", "music please"), a request phrase
  ("I want", "let's", "how about"), a number ("to 30", "50%"), or in a message
  of three words or fewer ("the fan") — so "tell me a fun fact about music" is
  not a request;
- a Home Assistant area name or a multi-word entity display name / object id
  from the controllable listing, with the same cues (no short-message pass;
  single-word entity names never count — Hue scenes are called "Relax",
  "Read", "Concentrate");
- a bare command: "off", "back on", "stop", "pause", "next", "68 degrees";
- a near-whole-message yes ("yes", "ok", "yes please", "go ahead") with no
  negation, when the assistant's previous message ENDS with its own question
  or offer about a device ("Want me to turn off the porch light?"). Quoted
  text, bulleted / numbered / time-prefixed readout lines and the end beyond
  ``_MAX_CHARS`` are ignored, and a spoken announcement (``[Jess announced -
  …]``, or text matching recent announcement history) is never treated as
  Jess's question — calendar titles are read out verbatim and anyone can send
  an invite.

How-to questions are not requests: the rest of a sentence after "how do I",
"how to", "tips for" … is not read ("how do I turn off the lights?").

Once someone else's text is in play this turn — a tool outside
``TRUSTED_RESULT_TOOLS`` has run, or the user's message carries a photo —
only an explicit request still counts: an action verb governing a device word
or name (or a confirmed yes). "Check my email and turn off the lights" still
works; "open the email about the heating bill" or "what's on TV" followed by
a web search does not.

Errs towards letting a real request through: a missed request costs one
question ("want me to …?" → "yes"), and the guard behind this still refuses
everything risky. Known limits:

- turn-level, not entity-level ("turn off the lights" lets the model touch
  any listed device that turn);
- negation is not parsed ("don't turn off the lights" counts as asking);
- text the user pastes into their own message counts as theirs;
- ordinary sentences still pass now and then ("I put on some weight", "the
  heat is too high today");
- indirect requests are refused ("I'm cold", "it's dark in here") — the model
  asks first;
- with real streaming, words the model wrote before a blocked call ("turning
  it off…") already reached the screen;
- the memory / RAG context in the system prompt is auto-learned text too, but
  doesn't make a turn strict (it is there on every turn — strict always would
  refuse a bare "lights off"); a planted memory still needs the user's own
  words to show device intent;
- the yes path needs the client to send the previous assistant turn (the
  dashboard and Telegram do), and trusts the model's own closing question:
  content the model rephrases into its last sentence (a calendar title read
  out inline, not as a list line) can still be what a "yes" answers.

``HA_INTENT_GATE`` = ``enforce`` (default) | ``shadow`` (count + log "would
block", still execute) | ``off``. Only the model-steered chat path uses this.
Internal callers (routine ``ha_action``, wind-down scenes, focus audio,
announcements), the HA fast path / NLP path and ``POST /api/ha/command``
never pass through it.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, List, Optional, Set

from orchestrator.ha_guard import HA_ALLOWED_SERVICES, HA_LISTED_DOMAINS, ha_device_setting, ha_tool_denied
from orchestrator.metrics import HA_INTENT_DECISION, HA_UNREQUESTED_BLOCKED

logger = logging.getLogger(__name__)

# Model-facing tool result for a blocked call. The model asks instead.
UNREQUESTED_RESULT = json.dumps(
    {
        "executed": False,
        "reason": "the user didn't ask for a device change — ask before acting",
        "note": "Nothing was changed. Don't call home_assistant again this turn. If a device change would "
        "help, ask first and name the device and the change, e.g. 'Want me to turn off the living room speaker?'",
    },
    ensure_ascii=False,
)

# Tools whose results hold only text the orchestrator writes or the user's
# own words. Every other tool may return someone else's text (web pages,
# mail, documents, sheets, photos, memories, calendar titles, task and pantry
# names from pasted or scanned content, payee names, logs) — after one of
# those runs, only an explicit request lets a device call through. An
# allow-list, so a new tool starts out untrusted.
TRUSTED_RESULT_TOOLS = frozenset(
    {
        "home_assistant",
        "helios_power",
        "start_focus",
        "stop_focus",
        "focus_status",
        "focus_sprint",
        "set_reminder",
        "cancel_reminder",
        "create_calendar_event",
        "selfcare_log",
        "sleep_mode",
        "voice_style",
        "get_data",
        "update_data",
        "log_set",
        "log_meal",
        "generate_workout",
        "workout_setup",
        "modify_workout",
    }
)

# Only the start of a user message (the end of an assistant one) is read: the
# regexes are linear, but a pasted 50k-char message would still cost tens of
# ms on the event loop.
_MAX_CHARS = 4000

# "how do I turn off the lights?" asks for a method, not an action: the rest
# of the sentence after a how-to phrase is not read as a request.
_HOWTO_CLAUSE_RE = re.compile(
    r"\b(?:how\s+(?:to|do\s+(?:i|you|we)|does\s+one|should\s+i|would\s+i)|learn(?:ing)?\s+to|ways?\s+to|"
    r"tips?\s+(?:to|for|on))\b[^.?!,;\n]*"
)

# Device words. Common non-device senses are carved out ("a light lunch", "in
# light of", "a fan of"), since this app's own conversations (meals,
# selfcare, tasks) are full of them.
_DEVICE_NOUNS = (
    r"lights|lighting|(?<!\bin\s)light(?!\s+(?:snack|meal|lunch|breakfast|dinner|reading|read|workout|"
    r"exercise|jog|run|rain|sleeper|weight|years?))|lamps?|bulbs?|led\s+strips?|fans?(?!\s+of\b)|speakers?|"
    r"music|songs?|playlists?|playback|radio|tv|tvs|television|telly|chromecast|sonos|thermostat|heat|heater|"
    r"heating|ac|a/c|aircon|air\s+con(?:ditioning|ditioner)?|hvac|blinds?|shades?|curtains?|garage|plugs?|"
    r"scenes?|volume|brightness|dimmer|humidifier|dehumidifier|purifier"
)
_NOUN_RE = re.compile(rf"\b(?:{_DEVICE_NOUNS})\b")
# A cue right after a device word: "lights off", "fan please". Not up/down:
# "is it going to heat up" is weather; "turn the heat up" has a verb anyway.
_NOUN_CUE_RE = re.compile(rf"\b(?:{_DEVICE_NOUNS})(?:\W+\w+){{0,3}}?\W+(?:on|off|please)\b")
_NAME_CUES = frozenset({"on", "off", "please"})
_NAME_CUE_WINDOW = 4

_COLORS = r"red|orange|yellow|green|blue|purple|pink|white|warm\s+white|cool\s+white"
_OBJ = r"(?:it|this|that|them|those|these|everything|all|both)"

# A verb governs a device word or name when it comes at most this many words
# before it, with none of these in between ("open the email about the
# heating bill", "make a list of the garage sale items" are not device
# requests), and isn't itself the topic of something ("an email about
# turning off the heat").
_GOVERN_WINDOW = 6
_GOVERN_BLOCKERS = frozenset(
    {
        "about",
        "for",
        "from",
        "of",
        "with",
        "regarding",
        "email",
        "emails",
        "mail",
        "message",
        "messages",
        "page",
        "article",
        "list",
        "review",
        "bill",
        "invoice",
        "post",
        "video",
        "story",
        "news",
        "recipe",
    }
)
_GOVERN_TOPIC_MARKERS = frozenset({"about", "regarding"})
# "turn off all of the lights": this "of" doesn't change the object.
_QUANTIFIERS = frozenset({"all", "both", "some", "each", "any", "most", "half"})

# Verbs that make a device word or name a request: "stop the music", "close
# the blinds", "start the fan", "set the bedroom to 40".
_ACTION_VERB_RE = re.compile(
    r"\b(?:turn(?:ed|ing|s)?|switch(?:ed|ing|es)?|flip|power|shut|put|set|change|adjust|make|start|stop|close|"
    r"raise|lower|open|kill|cut|bring|crank|bump|skip|restart|dim|brighten|mute|unmute|pause|resume|play|"
    r"activate|deactivate|toggle)\b"
)
_REQUEST_CUE_RE = re.compile(
    r"\b(?:i\s+want|i'd\s+like|i\s+would\s+like|let's|lets|how\s+about|too\s+(?:hot|cold|warm|chilly|high|low))\b"
    r"|\b(?:to|at)\s+\d{1,3}\b|\d+\s*(?:%|percent\b|degrees?\b|°)"
)

# Requests on their own, no device word needed.
_VERB_RES = (
    re.compile(r"\b(?:turn(?:ed|ing|s)?|switch(?:ed|ing|es)?|flip|power)\b(?:\s+\S+){0,5}?\s+(?:on|off)\b"),
    re.compile(r"\bshut\b(?:\s+\S+){0,4}?\s+off\b"),
    # up/down and put … on only with a pronoun object; with a device word or
    # name they are covered by the action-verb rule ("turned me down", "put
    # off my taxes", "put milk on the list" are not requests).
    re.compile(rf"\b(?:turn(?:ed|ing|s)?|switch|crank|bump|bring)\s+{_OBJ}\s+(?:up|down)\b"),
    re.compile(rf"\bput\s+(?:{_OBJ}\s+on|on\s+some)\b"),
    re.compile(r"\b(?:brighten|toggle|mute|unmute|unpause|louder|quieter|brighter)\b"),
    re.compile(r"\bdim\b(?!\s+sum\b)"),
    re.compile(
        r"\bpause\b(?!\s+(?:and|to|for|my|focus|timer|routine|session|the\s+(?:focus|timer|routine|session|task)))"
    ),
    re.compile(
        r"\bplay\b(?!\s+(?:a\s+|some\s+|the\s+)?(?:game|games|role|with|along|it\s+(?:safe|cool|by\s+ear)|"
        r"catch|pretend|chess|cards|nice|fair|dumb|video))"
    ),
    re.compile(r"\b(?:de)?activate\b(?!\s+(?:my\s+|the\s+)?(?:focus|sleep|routine|reminder|dnd|do\s+not|timer))"),
    re.compile(rf"\block\s+(?:{_OBJ}|up|the\s+(?!screen\b)\w+)\b"),
    re.compile(rf"\b(?:close|raise|lower)\s+{_OBJ}\b"),
    re.compile(
        r"\bset\b(?!(?:\s+\S+){0,3}?\s+(?:reminders?|timers?|alarms?|appointments?|meetings?|goals?|deadlines?|"
        r"events?)\b)(?:\s+\S+){0,4}?\s+(?:to|at)\s+\d"
    ),
    re.compile(r"\bmake\s+it\s+\d{2}\b"),
    re.compile(rf"\b(?:make|turn|change|set)\s+{_OBJ}\s+(?:to\s+)?(?:{_COLORS})\b"),
    re.compile(rf"\bmake\s+{_OBJ}\s+(?:brighter|darker|dimmer|louder|quieter|softer|warmer|cooler)\b"),
    re.compile(r"\b(?:all|everything|both)\s+(?:on|off)\b"),
    re.compile(r"\btoo\s+(?:loud|quiet|bright|dark)\b"),
    re.compile(r"(?:^|\b(?:play|skip(?:\s+to)?|go\s+to)\s+)(?:the\s+)?(?:next|previous)\s+(?:song|track)\b"),
)

# Whole-message commands (matched on the normalised words, user text only).
_SHORT_COMMAND_RES = (
    re.compile(rf"^(?:(?:turn|switch|flip|put)\s+)?(?:{_OBJ}\s+)?(?:back\s+)?(?:on|off)(?:\s+(?:please|now|again))?$"),
    re.compile(
        r"^(?:please\s+)?(?:stop|pause|mute|unmute|skip|resume|next|previous|shuffle)"
        r"(?:\s+(?:it|that|this|please|now|again|the\s+(?:music|song|track)))*$"
    ),
)
# "68 degrees", "50%" as a whole message. Not "100%" — that's "absolutely".
_NUMBER_ONLY_RE = re.compile(r"^\s*(?!100\s*(?:%|percent))\d{2,3}\s*(?:%|percent|degrees?|°)\s*[.!]*\s*$")
# A device word alone counts in a message this short ("the fan", "music").
_SHORT_NOUN_MESSAGE_WORDS = 3

# A yes is the whole message: "yes", "ok", "yes please", "yeah go ahead".
_AFFIRMATIVE_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|yea|ya|y|sure|ok|okay|k|alright|all\s+right|absolutely|definitely|of\s+course|"
    r"correct|go\s+ahead|go\s+for\s+it|do\s+it|please\s+do|sounds\s+good|perfect)"
    r"(?:\s+(?:yes|yeah|please|thanks|thank\s+you|do\s+it|go\s+ahead|sure|thing|ok|okay|great|perfect|"
    r"that\s+would\s+be\s+great|that\s+d\s+be\s+great))*$"
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|don't|dont|do\s+not|nope|nah|never|nvm|never\s+mind|cancel|stop|wait|leave\s+it|hold\s+off|later)\b"
)
_OFFER_RE = re.compile(
    r"\b(?:want\s+me\s+to|should\s+i|shall\s+i|would\s+you\s+like\s+me\s+to|do\s+you\s+want\s+me\s+to|"
    r"if\s+you\s+want|if\s+you'd\s+like|let\s+me\s+know\s+if)\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n+")
# Bulleted, numbered or time-prefixed lines in the assistant's message are a
# readout (calendar, tasks, search results), not its own question: a calendar
# title "Want me to turn off all the lights?" read out last must not count.
# Bullets, numbers, letters ("a)"), table rows and lines starting with a time
# all go, whole: an invite titled "Team sync. Want me to turn off all the
# lights?" read out as "15:00 Team sync. Want me to …" must not count — at the
# price of the model's own "10:30 is your meeting. Want me to dim the lights?"
# needing a second ask.
_LIST_LINE_RE = re.compile(r"(?m)^\s*(?:[-*•]\s|\||\d+[.)]|[a-z][.)]\s|\d{1,2}:\d{2}).*$")
# Closing pleasantries skipped when finding the message's last sentence.
_PLEASANTRY_RE = re.compile(
    r"^(?:just\s+)?(?:let\s+me\s+know|i\s+can\s+do\s+that|happy\s+to\s+help|no\s+problem|up\s+to\s+you|"
    r"your\s+call)\W*$"
)
# "Want the lights off?" — a device word with on/off right after it.
_NOUN_ONOFF_RE = re.compile(rf"\b(?:{_DEVICE_NOUNS})(?:\W+\w+){{0,3}}?\W+(?:on|off)\b")
# Quoted spans in the assistant's message are someone else's words.
_QUOTED_RES = (
    re.compile(r"(?m)^\s*>.*$"),
    re.compile(r"[\"“”][^\"“”\n]{0,600}[\"“”]"),
    re.compile(r"(?<!\w)'[^'\n]{1,600}'(?!\w)"),
    re.compile(r"`[^`]{0,600}`"),
)
# The dashboard resends spoken announcements as assistant turns, labelled
# "[Jess announced - calendar]: …" (frontend lib/chat-history.ts).
_ANNOUNCEMENT_LABEL_RE = re.compile(r"^\s*\[[^\]\n]{0,60}\bannounced\b[^\]\n]{0,60}\]", re.IGNORECASE)
_ANNOUNCEMENT_HISTORY_ROWS = 50
_ANNOUNCEMENT_MIN_CHARS = 12

# Names too generic to say anything on their own, even when an entity or area
# carries them.
_GENERIC_NAMES = frozenset({"home", "house", "all", "main", "default", "group", "everything", "other"})
_MIN_NAME_CHARS = 3

_name_lookup_failed_logged = False


def _lower(text: str) -> str:
    text = unicodedata.normalize("NFKC", (text or "")[:_MAX_CHARS]).lower()
    return text.replace("’", "'").replace("‘", "'")


def _words(text: str) -> str:
    """Lowercase alphanumeric words joined by single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _lower(text)).split())


def _name_phrases() -> Set[str]:
    """Area names plus multi-word entity names / object ids of the entities
    chat can control (the same set the tool schema lists). Empty when Home
    Assistant hasn't been read yet, or reading the caches fails — the verb and
    device-word checks still apply, so this fails towards refusing."""
    global _name_lookup_failed_logged
    from orchestrator import ha_registry, shared

    phrases: Set[str] = set()
    try:
        for meta in ha_registry.cached_meta().values():
            area = (meta or {}).get("area")
            if isinstance(area, str):
                phrases.add(_words(area))
        client = getattr(shared, "ha_client", None)
        if client is not None:
            for domain in HA_LISTED_DOMAINS:
                for entity in client.get_entities_by_domain(domain):
                    if ha_tool_denied(entity.entity_id) or ha_device_setting(entity.entity_id):
                        continue
                    for raw in (str(entity.friendly_name or ""), entity.entity_id.split(".", 1)[-1]):
                        phrase = _words(raw)
                        if len(phrase.split()) >= 2:
                            phrases.add(phrase)
    except Exception:  # noqa: BLE001 — a cache hiccup must not break the chat turn
        # Once per process: a persistent failure would otherwise put a
        # traceback in the self-audit digest on every gated turn.
        if not _name_lookup_failed_logged:
            _name_lookup_failed_logged = True
            logger.warning(
                "[HA-INTENT] Reading entity/area names failed — verb and device-word checks only", exc_info=True
            )
    return {p for p in phrases if len(p) >= _MIN_NAME_CHARS and p not in _GENERIC_NAMES and not p.isdigit()}


def _verb_governs_device(words: str, names: List[str]) -> bool:
    """An action verb shortly before a device word or name, nothing in
    between that moves the device word into another object's clause."""
    tokens = words.split()
    starts = {m.start() for m in _NOUN_RE.finditer(words)}
    padded = f" {words} "
    for name in names:
        i = padded.find(f" {name} ")
        while i != -1:
            starts.add(i)  # padded index of the leading space == words index of the name
            i = padded.find(f" {name} ", i + 1)
    for pos in starts:
        idx = words[:pos].count(" ")
        for j in range(idx - 1, max(-1, idx - 1 - _GOVERN_WINDOW), -1):
            if tokens[j] in _GOVERN_BLOCKERS and not (tokens[j] == "of" and j > 0 and tokens[j - 1] in _QUANTIFIERS):
                break
            if _ACTION_VERB_RE.fullmatch(tokens[j]):
                if j == 0 or tokens[j - 1] not in _GOVERN_TOPIC_MARKERS:
                    return True
                break
    return False


def _matched_names(words: str) -> List[str]:
    padded = f" {words} "
    return [p for p in _name_phrases() if f" {p} " in padded]


def _name_has_cue(words: str, names: List[str]) -> bool:
    """A cue word within a few words after a matched name ("kitchen off")."""
    padded = f" {words} "
    for name in names:
        start = 0
        while (i := padded.find(f" {name} ", start)) != -1:
            after = padded[i + len(name) + 2 :].split()[:_NAME_CUE_WINDOW]
            if _NAME_CUES.intersection(after):
                return True
            start = i + 1
    return False


@dataclass(frozen=True)
class Assessment:
    """Whether a message asks for a device change.

    ``via`` names the signal (verb, device_word, name, short, confirmed) or is
    None. ``explicit`` — an action verb governing a device word or name ("turn
    off the kitchen lights", not "an email about the heating bill") — is what
    still counts after untrusted content ran this turn."""

    via: Optional[str]
    explicit: bool = False

    def via_for(self, after_untrusted: bool) -> Optional[str]:
        if not after_untrusted or self.via == "confirmed" or self.explicit:
            return self.via
        return None


def _assess_text(text: str, *, allow_short: bool) -> Assessment:
    lowered = _HOWTO_CLAUSE_RE.sub(" ", _lower(text))
    words = _words(lowered)
    if not words:
        return Assessment(None)
    strong = any(r.search(lowered) for r in _VERB_RES)
    noun = bool(_NOUN_RE.search(lowered))
    names: Optional[List[str]] = None
    governed: Optional[bool] = None

    def _names() -> List[str]:
        nonlocal names
        if names is None:
            names = _matched_names(words)
        return names

    def _governed() -> bool:
        nonlocal governed
        if governed is None:
            governed = (noun or bool(_names())) and _verb_governs_device(words, _names())
        return governed

    via: Optional[str] = None
    if strong:
        via = "verb"
    elif allow_short and (any(r.match(words) for r in _SHORT_COMMAND_RES) or _NUMBER_ONLY_RE.match(lowered)):
        via = "short"
    elif noun and (
        _governed()
        or len(words.split()) <= _SHORT_NOUN_MESSAGE_WORDS
        or _NOUN_CUE_RE.search(lowered)
        or _REQUEST_CUE_RE.search(lowered)
    ):
        via = "verb" if _governed() else "device_word"
    elif _names() and (_governed() or _REQUEST_CUE_RE.search(lowered) or _name_has_cue(words, _names())):
        via = "verb" if _governed() else "name"
    explicit = via is not None and _governed()
    return Assessment(via, explicit)


def shows_device_intent(text: str) -> bool:
    """True when ``text`` (a user message) reads like a request to change a device."""
    return _assess_text(text, allow_short=True).via is not None


def _is_affirmative(text: str) -> bool:
    words = _words(text)
    return bool(words) and not _NEGATION_RE.search(words) and bool(_AFFIRMATIVE_RE.match(words))


def _is_announcement(text: str) -> bool:
    """An announcement resent as an assistant turn: labelled by the
    dashboard, or matching a recent spoken announcement. Fails towards True
    (no yes-path) if the history can't be read."""
    if _ANNOUNCEMENT_LABEL_RE.match(text):
        return True
    from orchestrator import state_store

    try:
        rows = state_store.get_announcement_history(limit=_ANNOUNCEMENT_HISTORY_ROWS)
    except Exception:  # noqa: BLE001
        logger.warning("[HA-INTENT] Announcement history unreadable — not treating the reply as a yes")
        return True
    said = _words(text)
    for row in rows:
        announced = _words(str(row.get("text") or ""))
        if len(announced) >= _ANNOUNCEMENT_MIN_CHARS and (
            announced in said or (len(said) >= _ANNOUNCEMENT_MIN_CHARS and said in announced)
        ):
            return True
    return False


def _asks_about_device(assistant_text: str) -> bool:
    """The assistant's message ENDS with its own question or offer about a
    device: a control verb ("want me to turn it off?"), an action verb
    governing a device word or name ("want me to resume playback?"), or a
    device word with on/off ("want the lights off?"). Read from the end, with
    quoted spans, readout lines and how-to clauses removed."""
    if not assistant_text or not assistant_text.strip() or _is_announcement(assistant_text):
        return False
    text = _lower(assistant_text[-_MAX_CHARS:])
    text = _LIST_LINE_RE.sub(" ", text)
    for quoted in _QUOTED_RES:
        text = quoted.sub(" ", text)
    text = _HOWTO_CLAUSE_RE.sub(" ", text)
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    # Skip a closing emoji or pleasantry ("…lights? Just let me know!").
    while sentences and (not re.search(r"[a-z]", sentences[-1]) or _PLEASANTRY_RE.match(sentences[-1])):
        sentences.pop()
    if not sentences:
        return False
    ask = sentences[-1]
    if not (ask.rstrip("\"')]*_ ").endswith("?") or _OFFER_RE.search(ask)):
        return False
    if any(r.search(ask) for r in _VERB_RES) or _NOUN_ONOFF_RE.search(ask):
        return True
    words = _words(ask)
    return _verb_governs_device(words, _matched_names(words))


def assess(user_text: str, assistant_before: str = "") -> Assessment:
    """Does this turn ask for a device change? ``user_text`` is the user's
    latest message; ``assistant_before`` the assistant message right before
    it ("" if none) — what a bare yes is answering."""
    result = _assess_text(user_text, allow_short=True)
    if result.via is None and _is_affirmative(user_text) and _asks_about_device(assistant_before):
        return Assessment("confirmed")
    return result


def device_change_requested(user_text: str, assistant_before: str = "") -> bool:
    return assess(user_text, assistant_before).via is not None


def check_call(assessment: Assessment, arguments: Any, label: str, *, after_untrusted: bool, first_block: bool) -> bool:
    """Decide one model ``home_assistant`` call: True = refuse it.

    Counts every decision; logs a refusal (WARNING for the turn's first,
    INFO after). ``HA_INTENT_GATE`` shadow mode only counts and logs."""
    from orchestrator import shared

    mode = getattr(shared, "HA_INTENT_GATE", "enforce")
    if mode == "off":
        return False
    via = assessment.via_for(after_untrusted)
    if via is not None:
        HA_INTENT_DECISION.labels(result="allowed", via=via).inc()
        return False
    reason = "untrusted_content" if assessment.via is not None else "none"
    shadow = mode == "shadow"
    HA_INTENT_DECISION.labels(result="shadow" if shadow else "blocked", via=reason).inc()
    if not shadow:
        HA_UNREQUESTED_BLOCKED.labels(domain=_domain(arguments)).inc()
    try:
        _log_refusal(arguments, label, reason=reason, shadow=shadow, first=first_block)
    except Exception:  # noqa: BLE001 — logging must never undo the refusal
        logger.debug("[HA-INTENT] Refusal log failed", exc_info=True)
    return not shadow


def _domain(arguments: Any) -> str:
    entity_id = arguments.get("entity_id") if isinstance(arguments, dict) else None
    domain = entity_id.split(".", 1)[0] if isinstance(entity_id, str) and "." in entity_id else ""
    return domain if domain in HA_LISTED_DOMAINS else "other"


def _log_refusal(arguments: Any, label: str, *, reason: str, shadow: bool, first: bool) -> None:
    """Ids only, and only ids Home Assistant actually has: the model can emit
    any string, and WARNING lines reach the self-audit LLM."""
    from orchestrator import shared

    args = arguments if isinstance(arguments, dict) else {}
    entity_id = args.get("entity_id")
    service = args.get("service")
    domain = _domain(arguments)
    client = getattr(shared, "ha_client", None)
    known = isinstance(entity_id, str) and client is not None and client.get_entity(entity_id) is not None
    entity = entity_id if known else "-"
    svc = service if isinstance(service, str) and service in HA_ALLOWED_SERVICES.get(domain, ()) else "-"
    if shadow:
        logger.info(
            "[%s] Would block unrequested home_assistant call (shadow mode, reason=%s): domain=%s entity=%s service=%s",
            label,
            reason,
            domain,
            entity,
            svc,
        )
        return
    # The turn's first refusal is worth a look in the self-audit digest; a
    # model that keeps trying in the same turn shouldn't flood it.
    fmt = "[%s] Blocked unrequested home_assistant call (reason=%s): domain=%s entity=%s service=%s"
    if first:
        logger.warning(fmt, label, reason, domain, entity, svc)
    else:
        logger.info(fmt, label, reason, domain, entity, svc)
