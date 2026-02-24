"""Config flow for Brain Gateway."""

import aiohttp
import voluptuous as vol

from homeassistant import config_entries

from .const import CONF_URL, DEFAULT_URL, DOMAIN


class BrainGatewayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Brain Gateway."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            # Validate connectivity
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{url}/health",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        data = await resp.json()
                        if not data.get("ok"):
                            errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

            if not errors:
                return self.async_create_entry(
                    title="Brain Gateway",
                    data={CONF_URL: url},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): str,
                }
            ),
            errors=errors,
        )
