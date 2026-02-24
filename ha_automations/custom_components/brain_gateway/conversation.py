"""Brain Gateway conversation agent."""

import logging

from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

MAX_HISTORY = 20  # Keep last 10 turns (20 messages)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Brain Gateway conversation entity."""
    url = entry.data[CONF_URL]
    async_add_entities([BrainGatewayConversationEntity(hass, entry, url)])


class BrainGatewayConversationEntity(ConversationEntity):
    """Brain Gateway conversation agent entity.

    Routes voice input through the hybrid orchestrator (Helios + Nemotron)
    instead of hitting a single model directly.
    """

    _attr_has_entity_name = True
    _attr_name = "Brain Gateway"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, url: str) -> None:
        """Initialize the conversation entity."""
        self.hass = hass
        self._entry = entry
        self._url = url
        self._conversations: dict[str, list[dict]] = {}
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str]:
        """Return supported languages."""
        return ["en"]

    async def async_process(self, user_input) -> ConversationResult:
        """Process a sentence."""
        conv_id = user_input.conversation_id or "default"
        if conv_id not in self._conversations:
            self._conversations[conv_id] = []

        messages = self._conversations[conv_id]
        messages.append({"role": "user", "content": user_input.text})

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{self._url}/v1/chat/completions",
                json={"messages": messages, "stream": False},
                timeout=120,
            ) as resp:
                data = await resp.json()
                response_text = data["choices"][0]["message"]["content"]

            _LOGGER.debug(
                "Brain Gateway response (conv=%s): %s", conv_id, response_text[:200]
            )
        except Exception:
            _LOGGER.exception("Brain Gateway call failed")
            response_text = "Sorry, I couldn't reach the brain gateway."

        messages.append({"role": "assistant", "content": response_text})

        # Trim conversation history
        if len(messages) > MAX_HISTORY:
            self._conversations[conv_id] = messages[-MAX_HISTORY:]

        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(response_text)
        return ConversationResult(response=response, conversation_id=conv_id)
