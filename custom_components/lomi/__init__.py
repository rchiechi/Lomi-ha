"""The Lomi integration (cloud polling via api.lomi-app.net)."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import LomiAuth, LomiAuthError, LomiClient, LomiError, LomiTokens
from .const import CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import LomiConfigEntry, LomiCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain services (they resolve the target entry per call)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LomiConfigEntry) -> bool:
    """Set up Lomi from a config entry."""
    session = async_get_clientsession(hass)

    @callback
    def _tokens_updated(tokens: LomiTokens) -> None:
        # Persist a rotated refresh token if Cognito ever hands us one.
        if tokens.refresh_token and tokens.refresh_token != entry.data.get(
            CONF_REFRESH_TOKEN
        ):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: tokens.refresh_token}
            )

    auth = LomiAuth(
        session,
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        email=entry.data.get(CONF_EMAIL),
        password=entry.data.get(CONF_PASSWORD),
        on_tokens_updated=_tokens_updated,
    )
    client = LomiClient(session, auth)

    try:
        await auth.async_get_id_token()
    except LomiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except LomiError as err:
        raise ConfigEntryNotReady(f"Cannot reach Lomi cloud: {err}") from err

    coordinator = LomiCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: LomiConfigEntry) -> None:
    """Reload when options change (not when a rotated token lands in data)."""
    coordinator = entry.runtime_data
    if dict(entry.options) != coordinator.options_snapshot:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LomiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
