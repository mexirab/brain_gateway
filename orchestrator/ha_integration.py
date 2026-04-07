"""
Home Assistant Integration Module v2
- Auto-discovers all entities from HA REST API
- Fuzzy matches user speech to entity names
- Supports lights (including COLOR!), switches, media players, scenes, climate
- No hardcoded entity mappings!

Usage:
    from ha_integration import HomeAssistantClient

    ha = HomeAssistantClient(url="http://your-ha-host:8123", token="...")
    await ha.refresh_entities()  # Call once at startup

    result = await ha.execute_command("turn on the living room lights")
    result = await ha.execute_command("turn the bedroom red")
    result = await ha.execute_command("set office to blue")
"""

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

# Try to import rapidfuzz for better matching, fall back to simple matching
try:
    from rapidfuzz import fuzz, process

    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False
    print("[ha_integration] Warning: rapidfuzz not installed. Using basic matching.")
    print("  Install with: pip install rapidfuzz")


# Color name to RGB mapping
COLOR_MAP = {
    # Basic colors
    "red": [255, 0, 0],
    "green": [0, 255, 0],
    "blue": [0, 0, 255],
    "yellow": [255, 255, 0],
    "orange": [255, 165, 0],
    "purple": [128, 0, 128],
    "pink": [255, 192, 203],
    "white": [255, 255, 255],
    "cyan": [0, 255, 255],
    "magenta": [255, 0, 255],
    # Extended colors
    "warm white": [255, 244, 229],
    "cool white": [255, 255, 255],
    "daylight": [255, 255, 251],
    "soft white": [255, 244, 229],
    "warm": [255, 244, 229],
    "cool": [255, 255, 255],
    # Mood colors
    "lavender": [230, 230, 250],
    "coral": [255, 127, 80],
    "teal": [0, 128, 128],
    "turquoise": [64, 224, 208],
    "gold": [255, 215, 0],
    "salmon": [250, 128, 114],
    "lime": [0, 255, 0],
    "aqua": [0, 255, 255],
    "violet": [238, 130, 238],
    "indigo": [75, 0, 130],
    # Scene-like colors
    "sunset": [255, 100, 50],
    "sunrise": [255, 200, 150],
    "ocean": [0, 105, 148],
    "forest": [34, 139, 34],
    "fire": [255, 69, 0],
    "ice": [200, 233, 233],
    "romantic": [255, 105, 180],
    "party": [255, 0, 255],
    "relax": [255, 200, 150],
    "focus": [255, 255, 255],
    "energize": [255, 255, 200],
    "night": [50, 50, 100],
    "movie": [50, 50, 80],
}

# Build list of color names for regex
COLOR_NAMES = "|".join(sorted(COLOR_MAP.keys(), key=len, reverse=True))


@dataclass
class Entity:
    """Represents a Home Assistant entity."""

    entity_id: str
    domain: str
    friendly_name: str
    state: str
    attributes: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_ha_state(cls, data: Dict[str, Any]) -> "Entity":
        entity_id = data.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else "unknown"
        attrs = data.get("attributes", {})
        friendly_name = attrs.get("friendly_name", entity_id)

        return cls(
            entity_id=entity_id,
            domain=domain,
            friendly_name=friendly_name,
            state=data.get("state", "unknown"),
            attributes=attrs,
        )


@dataclass
class ParsedCommand:
    """Result of parsing a user command."""

    action: str  # turn_on, turn_off, toggle, set_brightness, set_color, etc.
    domain: str  # light, switch, media_player, scene, climate
    target_text: str  # What the user said ("living room lights")
    value: Optional[Any] = None  # brightness=50, color="red", temperature=72, etc.
    raw_text: str = ""


@dataclass
class ExecutionResult:
    """Result of executing a command."""

    success: bool
    action: str
    entity_id: str
    message: str
    details: Optional[Dict[str, Any]] = None


class HomeAssistantClient:
    """
    Smart Home Assistant client with auto-discovery and fuzzy matching.
    """

    # Domains we can control
    CONTROLLABLE_DOMAINS = {"light", "switch", "media_player", "scene", "climate", "cover", "fan", "lock", "vacuum"}

    # Command patterns - order matters (more specific first)
    COMMAND_PATTERNS = [
        # COLOR + BRIGHTNESS combined (most specific - must be first!)
        # "set bedroom to blue at 50%" or "turn living room red at 75%"
        (
            rf"(?:turn|set|change|make)\s+(?:the\s+)?(.+?)\s+(?:to\s+)?({COLOR_NAMES})\s+(?:at\s+)?(\d+)\s*%",
            "set_color_brightness",
        ),
        (
            rf"(?:set|change|make)\s+(?:the\s+)?(.+?)\s+(?:color\s+)?(?:to\s+)?({COLOR_NAMES})\s+(?:at\s+)?(\d+)\s*%",
            "set_color_brightness",
        ),
        # COLOR commands (must be before basic on/off to catch "turn living room red")
        (rf"(?:turn|set|change|make)\s+(?:the\s+)?(.+?)\s+(?:to\s+)?({COLOR_NAMES})$", "set_color"),
        (rf"(?:set|change|make)\s+(?:the\s+)?(.+?)\s+(?:color\s+)?(?:to\s+)?({COLOR_NAMES})$", "set_color"),
        (rf"({COLOR_NAMES})\s+(?:for\s+)?(?:the\s+)?(.+)$", "set_color_reversed"),  # "red for living room"
        # Brightness commands
        (r"(?:set|dim|change)\s+(?:the\s+)?(.+?)\s+(?:to\s+)?(\d+)\s*%", "set_brightness"),
        (r"(?:dim|brighten)\s+(?:the\s+)?(.+?)\s+(?:to\s+)?(\d+)\s*%?", "set_brightness"),
        (r"(.+?)\s+(?:to\s+)?(\d+)\s*%", "set_brightness"),
        # Temperature commands
        (r"set\s+(?:the\s+)?(?:thermostat|temperature|temp)\s+(?:to\s+)?(\d+)", "set_temperature"),
        (r"set\s+(?:the\s+)?(.+?)\s+(?:to\s+)?(\d+)\s*(?:degrees|°)", "set_temperature"),
        # Volume commands
        (r"(?:set\s+)?(?:the\s+)?(.+?)\s+volume\s+(?:to\s+)?(\d+)\s*%?", "set_volume"),
        (r"volume\s+(?:on\s+)?(?:the\s+)?(.+?)\s+(?:to\s+)?(\d+)\s*%?", "set_volume"),
        # Media commands
        (r"(?:play|resume)\s+(?:the\s+)?(?:music\s+)?(?:on\s+)?(?:the\s+)?(.+)", "media_play"),
        (r"pause\s+(?:the\s+)?(?:music\s+)?(?:on\s+)?(?:the\s+)?(.+)", "media_pause"),
        (r"stop\s+(?:the\s+)?(?:music\s+)?(?:on\s+)?(?:the\s+)?(.+)", "media_stop"),
        (r"(?:next|skip)\s+(?:track\s+)?(?:on\s+)?(?:the\s+)?(.+)", "media_next"),
        (r"(?:previous|prev|back)\s+(?:track\s+)?(?:on\s+)?(?:the\s+)?(.+)", "media_previous"),
        # Scene commands
        (r"(?:activate|set|turn\s+on)\s+(?:the\s+)?(.+?)\s+scene", "activate_scene"),
        (r"(?:activate|set)\s+(?:the\s+)?scene\s+(.+)", "activate_scene"),
        # Basic on/off/toggle
        (r"turn\s+on\s+(?:the\s+)?(.+)", "turn_on"),
        (r"turn\s+off\s+(?:the\s+)?(.+)", "turn_off"),
        (r"switch\s+on\s+(?:the\s+)?(.+)", "turn_on"),
        (r"switch\s+off\s+(?:the\s+)?(.+)", "turn_off"),
        (r"toggle\s+(?:the\s+)?(.+)", "toggle"),
        # Lock commands
        (r"lock\s+(?:the\s+)?(.+)", "lock"),
        (r"unlock\s+(?:the\s+)?(.+)", "unlock"),
        # Cover commands
        (r"open\s+(?:the\s+)?(.+)", "open_cover"),
        (r"close\s+(?:the\s+)?(.+)", "close_cover"),
    ]

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        cache_ttl_seconds: int = 300,  # Refresh entity cache every 5 minutes
    ):
        self.url = (url or os.environ.get("HA_URL", "")).rstrip("/")
        self.token = token or os.environ.get("HA_TOKEN", "")
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)

        # Entity cache
        self._entities: Dict[str, Entity] = {}
        self._entities_by_domain: Dict[str, List[Entity]] = {}
        self._last_refresh: Optional[datetime] = None

        # For fuzzy matching: friendly_name -> entity_id
        self._name_to_entity: Dict[str, str] = {}

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def refresh_entities(self, force: bool = False) -> int:
        """
        Fetch all entities from Home Assistant and cache them.
        Returns the number of entities loaded.
        """
        # Check if cache is still valid
        if not force and self._last_refresh and datetime.now() - self._last_refresh < self.cache_ttl:
            return len(self._entities)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{self.url}/api/states", headers=self._headers)
                resp.raise_for_status()
                states = resp.json()
        except Exception as e:
            print(f"[ha_integration] Error fetching entities: {e}")
            return len(self._entities)  # Return existing cache size

        # Clear and rebuild cache
        self._entities.clear()
        self._entities_by_domain.clear()
        self._name_to_entity.clear()

        for state_data in states:
            entity = Entity.from_ha_state(state_data)
            self._entities[entity.entity_id] = entity

            # Group by domain
            if entity.domain not in self._entities_by_domain:
                self._entities_by_domain[entity.domain] = []
            self._entities_by_domain[entity.domain].append(entity)

            # Build name mapping for fuzzy matching
            # Use multiple variations for better matching
            name_lower = entity.friendly_name.lower()
            self._name_to_entity[name_lower] = entity.entity_id

            # Also add without common suffixes for easier matching
            for suffix in [" light", " lights", " switch", " lamp"]:
                if name_lower.endswith(suffix):
                    base = name_lower[: -len(suffix)]
                    self._name_to_entity[base] = entity.entity_id

        self._last_refresh = datetime.now()
        return len(self._entities)

    def get_entities_by_domain(self, domain: str) -> List[Entity]:
        """Get all entities for a domain (light, switch, etc.)"""
        return self._entities_by_domain.get(domain, [])

    def get_all_controllable(self) -> Dict[str, List[Entity]]:
        """Get all controllable entities grouped by domain."""
        return {
            domain: entities
            for domain, entities in self._entities_by_domain.items()
            if domain in self.CONTROLLABLE_DOMAINS
        }

    def _fuzzy_match_entity(self, target_text: str, preferred_domain: Optional[str] = None) -> Optional[Entity]:
        """
        Find the best matching entity for the given text.
        Uses fuzzy matching to handle variations like:
        - "living room" -> "Living Room Lights"
        - "kitchen light" -> "Kitchen Light"
        """
        target_lower = target_text.lower().strip()

        # Build candidate list based on preferred domain
        if preferred_domain and preferred_domain in self._entities_by_domain:
            candidates = {e.friendly_name.lower(): e.entity_id for e in self._entities_by_domain[preferred_domain]}
        else:
            # Search controllable domains
            candidates = {}
            for domain in self.CONTROLLABLE_DOMAINS:
                for entity in self._entities_by_domain.get(domain, []):
                    candidates[entity.friendly_name.lower()] = entity.entity_id

        if not candidates:
            return None

        # Try exact match first
        if target_lower in candidates:
            return self._entities.get(candidates[target_lower])

        # Try exact match in full name mapping
        if target_lower in self._name_to_entity:
            entity_id = self._name_to_entity[target_lower]
            return self._entities.get(entity_id)

        # Fuzzy match
        if HAS_RAPIDFUZZ:
            # Use rapidfuzz for better matching
            result = process.extractOne(
                target_lower,
                candidates.keys(),
                scorer=fuzz.WRatio,
                score_cutoff=60,  # Minimum 60% match
            )
            if result:
                matched_name, score, _ = result
                entity_id = candidates[matched_name]
                return self._entities.get(entity_id)
        else:
            # Simple substring matching fallback
            for name, entity_id in candidates.items():
                if target_lower in name or name in target_lower:
                    return self._entities.get(entity_id)

        return None

    def parse_command(self, text: str) -> Optional[ParsedCommand]:
        """
        Parse a natural language command into structured data.

        Examples:
        - "turn on the living room lights" -> ParsedCommand(action="turn_on", target="living room lights")
        - "set bedroom to 50%" -> ParsedCommand(action="set_brightness", target="bedroom", value=50)
        - "turn the living room red" -> ParsedCommand(action="set_color", target="living room", value="red")
        - "activate movie scene" -> ParsedCommand(action="activate_scene", target="movie")
        """
        text_lower = text.lower().strip()

        for pattern, action in self.COMMAND_PATTERNS:
            match = re.match(pattern, text_lower)
            if match:
                groups = match.groups()

                # Handle reversed color command ("red for living room")
                if action == "set_color_reversed":
                    color = groups[0]
                    target = groups[1]
                    return ParsedCommand(
                        action="set_color",
                        domain="light",
                        target_text=target.strip(),
                        value=color,
                        raw_text=text,
                    )

                # Handle color + brightness combo ("set bedroom to blue at 50%")
                if action == "set_color_brightness":
                    return ParsedCommand(
                        action="set_color_brightness",
                        domain="light",
                        target_text=groups[0].strip(),
                        value={"color": groups[1], "brightness": int(groups[2])},
                        raw_text=text,
                    )

                # Extract target and optional value
                target = groups[0] if groups else ""
                value = None

                if len(groups) > 1 and groups[1]:
                    # For color commands, value is the color name
                    if action == "set_color":
                        value = groups[1]
                    else:
                        try:
                            value = int(groups[1])
                        except ValueError:
                            value = groups[1]

                # Determine domain hint from action
                domain_hint = self._action_to_domain_hint(action)

                return ParsedCommand(
                    action=action,
                    domain=domain_hint,
                    target_text=target.strip(),
                    value=value,
                    raw_text=text,
                )

        return None

    def _action_to_domain_hint(self, action: str) -> str:
        """Map action to likely domain."""
        action_domains = {
            "turn_on": "light",  # Could also be switch
            "turn_off": "light",
            "toggle": "light",
            "set_brightness": "light",
            "set_color": "light",
            "set_color_brightness": "light",
            "set_temperature": "climate",
            "set_volume": "media_player",
            "media_play": "media_player",
            "media_pause": "media_player",
            "media_stop": "media_player",
            "media_next": "media_player",
            "media_previous": "media_player",
            "activate_scene": "scene",
            "lock": "lock",
            "unlock": "lock",
            "open_cover": "cover",
            "close_cover": "cover",
        }
        return action_domains.get(action, "light")

    def _split_compound_commands(self, text: str) -> List[str]:
        """
        Split compound commands on conjunctions like 'and', 'then', 'also'.

        Examples:
        - "turn off office and turn off kitchen" -> ["turn off office", "turn off kitchen"]
        - "turn on living room and set it to 50%" -> ["turn on living room", "set it to 50%"]
        """
        # Split on " and ", " then ", " also ", " plus " when followed by a command verb
        # This pattern looks for conjunction followed by common command verbs
        command_verbs = r"(?:turn|switch|toggle|set|dim|brighten|activate|play|pause|stop|lock|unlock|open|close)"
        split_pattern = rf"\s+(?:and|then|also|plus)\s+(?={command_verbs})"

        parts = re.split(split_pattern, text.strip(), flags=re.IGNORECASE)
        return [p.strip() for p in parts if p.strip()]

    async def execute_command(self, text: str) -> ExecutionResult:
        """
        Parse and execute a natural language command (or multiple commands).

        This is the main entry point for the orchestrator.
        Supports compound commands like "turn off office and turn off kitchen".
        """
        # Ensure entities are loaded
        if not self._entities:
            await self.refresh_entities()

        # Split compound commands
        commands = self._split_compound_commands(text)

        # If multiple commands, execute each and aggregate results
        if len(commands) > 1:
            results = []
            all_success = True
            messages = []

            for cmd_text in commands:
                result = await self._execute_single_command(cmd_text)
                results.append(result)
                if not result.success:
                    all_success = False
                messages.append(result.message)

            return ExecutionResult(
                success=all_success,
                action="multiple",
                entity_id=", ".join(r.entity_id for r in results if r.entity_id),
                message=" | ".join(messages),
                details={"commands": len(commands), "results": [r.message for r in results]},
            )

        # Single command
        return await self._execute_single_command(text)

    async def _execute_single_command(self, text: str) -> ExecutionResult:
        """Execute a single parsed command."""
        # Parse the command
        parsed = self.parse_command(text)
        if not parsed:
            return ExecutionResult(
                success=False,
                action="unknown",
                entity_id="",
                message=f"Could not understand command: {text}",
            )

        # Find the target entity
        entity = self._fuzzy_match_entity(parsed.target_text, parsed.domain)

        # If not found in preferred domain, try others
        if not entity and parsed.domain:
            entity = self._fuzzy_match_entity(parsed.target_text, None)

        if not entity:
            return ExecutionResult(
                success=False,
                action=parsed.action,
                entity_id="",
                message=f"Could not find device matching '{parsed.target_text}'",
            )

        # Execute the action
        return await self._execute_action(parsed, entity)

    async def _execute_action(self, cmd: ParsedCommand, entity: Entity) -> ExecutionResult:
        """Execute a parsed command on a specific entity."""

        domain = entity.domain
        service = self._get_service_for_action(cmd.action, domain)

        if not service:
            return ExecutionResult(
                success=False,
                action=cmd.action,
                entity_id=entity.entity_id,
                message=f"Unsupported action '{cmd.action}' for domain '{domain}'",
            )

        # Build service data
        service_data: Dict[str, Any] = {"entity_id": entity.entity_id}

        # Add value parameters based on action
        if cmd.action == "set_brightness" and cmd.value is not None:
            # HA uses 0-255 for brightness
            brightness = int((cmd.value / 100) * 255)
            service_data["brightness"] = brightness
        elif cmd.action == "set_color_brightness" and isinstance(cmd.value, dict):
            color_name = cmd.value["color"].lower()
            if color_name in COLOR_MAP:
                service_data["rgb_color"] = COLOR_MAP[color_name]
            else:
                return ExecutionResult(
                    success=False,
                    action=cmd.action,
                    entity_id=entity.entity_id,
                    message=f"Unknown color: {cmd.value['color']}",
                )
            service_data["brightness"] = int((cmd.value["brightness"] / 100) * 255)
        elif cmd.action == "set_color" and cmd.value is not None:
            # Get RGB from color name
            color_name = cmd.value.lower()
            if color_name in COLOR_MAP:
                service_data["rgb_color"] = COLOR_MAP[color_name]
            else:
                return ExecutionResult(
                    success=False,
                    action=cmd.action,
                    entity_id=entity.entity_id,
                    message=f"Unknown color: {cmd.value}",
                )
        elif cmd.action == "set_temperature" and cmd.value is not None:
            service_data["temperature"] = cmd.value
        elif cmd.action == "set_volume" and cmd.value is not None:
            # HA uses 0-1 for volume
            service_data["volume_level"] = cmd.value / 100

        # Make the API call
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                url = f"{self.url}/api/services/{domain}/{service}"
                resp = await client.post(url, headers=self._headers, json=service_data)
                resp.raise_for_status()

                # Build descriptive message
                if cmd.action == "set_color_brightness" and isinstance(cmd.value, dict):
                    msg = f"✓ Set {entity.friendly_name} to {cmd.value['color']} at {cmd.value['brightness']}%"
                elif cmd.action == "set_color":
                    msg = f"✓ Set {entity.friendly_name} to {cmd.value}"
                elif cmd.action == "set_brightness":
                    msg = f"✓ Set {entity.friendly_name} to {cmd.value}%"
                else:
                    msg = f"✓ {service.replace('_', ' ')} {entity.friendly_name}"

                return ExecutionResult(
                    success=True,
                    action=f"{service} {entity.entity_id}",
                    entity_id=entity.entity_id,
                    message=msg,
                    details={"service_data": service_data},
                )
        except Exception as e:
            return ExecutionResult(
                success=False,
                action=cmd.action,
                entity_id=entity.entity_id,
                message=f"Failed to execute: {e}",
            )

    def _get_service_for_action(self, action: str, domain: str) -> Optional[str]:
        """Map action to HA service name."""

        # Direct mappings
        direct_services = {
            "turn_on": "turn_on",
            "turn_off": "turn_off",
            "toggle": "toggle",
            "lock": "lock",
            "unlock": "unlock",
            "open_cover": "open_cover",
            "close_cover": "close_cover",
        }

        if action in direct_services:
            return direct_services[action]

        # Domain-specific mappings
        if domain == "light":
            if action == "set_brightness":
                return "turn_on"  # Brightness is a parameter of turn_on
            if action == "set_color":
                return "turn_on"  # Color is also a parameter of turn_on
            if action == "set_color_brightness":
                return "turn_on"  # Color + brightness are parameters of turn_on

        if domain == "climate" and action == "set_temperature":
            return "set_temperature"

        if domain == "media_player":
            media_services = {
                "media_play": "media_play",
                "media_pause": "media_pause",
                "media_stop": "media_stop",
                "media_next": "media_next_track",
                "media_previous": "media_previous_track",
                "set_volume": "volume_set",
            }
            return media_services.get(action)

        if domain == "scene" and action in ("turn_on", "activate_scene"):
            return "turn_on"

        return None

    async def call_service(self, entity_id: str, service: str, data: Dict[str, Any] = None) -> ExecutionResult:
        """
        Call a Home Assistant service directly (no NLP parsing).

        This is the simplified interface for when Nemotron provides structured calls.

        Args:
            entity_id: e.g., "light.bedroom_fan_lights"
            service: e.g., "turn_on", "turn_off", "toggle"
            data: Optional service data, e.g., {"brightness": 128, "rgb_color": [0,0,255]}
        """
        if not entity_id or not service:
            return ExecutionResult(
                success=False,
                action="unknown",
                entity_id=entity_id or "",
                message="Missing entity_id or service",
            )

        # Validate entity_id and service to prevent URL injection
        import re

        if not re.match(r"^[a-z_]+\.[a-z0-9_]+$", entity_id):
            return ExecutionResult(
                success=False,
                action=service or "unknown",
                entity_id=entity_id,
                message=f"Invalid entity_id format: {entity_id}",
            )
        if not re.match(r"^[a-z_]+$", service):
            return ExecutionResult(
                success=False,
                action=service or "unknown",
                entity_id=entity_id,
                message=f"Invalid service format: {service}",
            )

        # Extract domain from entity_id
        domain = entity_id.split(".")[0]

        # Build service data
        service_data = {"entity_id": entity_id}
        if data:
            service_data.update(data)

        # Make the API call
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                url = f"{self.url}/api/services/{domain}/{service}"
                resp = await client.post(url, headers=self._headers, json=service_data)
                resp.raise_for_status()

                # Build descriptive message
                entity = self._entities.get(entity_id)
                friendly_name = entity.friendly_name if entity else entity_id

                if data and "rgb_color" in data:
                    msg = f"✓ Set {friendly_name} to color {data['rgb_color']}"
                    if "brightness" in data:
                        pct = int((data["brightness"] / 255) * 100)
                        msg += f" at {pct}%"
                elif data and "brightness" in data:
                    pct = int((data["brightness"] / 255) * 100)
                    msg = f"✓ Set {friendly_name} to {pct}%"
                else:
                    msg = f"✓ {service.replace('_', ' ')} {friendly_name}"

                return ExecutionResult(
                    success=True,
                    action=f"{domain}.{service}",
                    entity_id=entity_id,
                    message=msg,
                    details={"service_data": service_data},
                )
        except httpx.HTTPStatusError as e:
            return ExecutionResult(
                success=False,
                action=f"{domain}.{service}",
                entity_id=entity_id,
                message=f"HA API error: {e.response.status_code} - {e.response.text[:100]}",
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                action=f"{domain}.{service}",
                entity_id=entity_id,
                message=f"Failed: {str(e)}",
            )

    async def get_entity_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Get current state of a specific entity."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.url}/api/states/{entity_id}",
                    headers=self._headers,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            print(f"[ha_integration] Error getting state for {entity_id}: {e}")
            return None

    def format_entity_summary(self) -> str:
        """Get a formatted summary of all controllable entities."""
        lines = ["Home Assistant Entities:"]

        for domain in sorted(self.CONTROLLABLE_DOMAINS):
            entities = self._entities_by_domain.get(domain, [])
            if entities:
                lines.append(f"\n{domain.upper()} ({len(entities)}):")
                for e in sorted(entities, key=lambda x: x.friendly_name):
                    state_info = f" [{e.state}]" if e.state not in ("unavailable", "unknown") else ""
                    lines.append(f"  • {e.friendly_name}{state_info}")

        return "\n".join(lines)

    def get_available_colors(self) -> List[str]:
        """Return list of supported color names."""
        return sorted(COLOR_MAP.keys())


# Convenience function for quick testing
async def test_connection(url: str = None, token: str = None) -> bool:
    """Test if Home Assistant is reachable."""
    client = HomeAssistantClient(url=url, token=token)
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(f"{client.url}/api/", headers=client._headers)
            return resp.status_code == 200
    except Exception:
        return False


# CLI for testing
if __name__ == "__main__":
    import sys

    async def main():
        client = HomeAssistantClient()

        print("Testing Home Assistant connection...")
        if await test_connection():
            print("✓ Connected!\n")
        else:
            print("✗ Connection failed. Check HA_URL and HA_TOKEN.")
            sys.exit(1)

        print("Loading entities...")
        count = await client.refresh_entities()
        print(f"✓ Loaded {count} entities\n")

        print(client.format_entity_summary())

        print(f"\nSupported colors: {', '.join(client.get_available_colors())}\n")

        # Interactive test
        print("\n" + "=" * 50)
        print("Test commands (type 'quit' to exit):")
        print("  Examples: 'turn on living room lights'")
        print("            'dim bedroom to 50%'")
        print("            'turn the office red'")
        print("            'set living room to blue'")
        print("            'activate movie scene'")
        print("=" * 50 + "\n")

        while True:
            try:
                cmd = input("Command> ").strip()
                if cmd.lower() in ("quit", "exit", "q"):
                    break
                if not cmd:
                    continue

                result = await client.execute_command(cmd)
                if result.success:
                    print(f"  ✓ {result.message}")
                    if result.details:
                        print(f"    Details: {result.details}")
                else:
                    print(f"  ✗ {result.message}")
            except KeyboardInterrupt:
                break
            except EOFError:
                break

        print("\nDone!")

    asyncio.run(main())
