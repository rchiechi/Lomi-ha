"""Config flow for the Lomi integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    LomiApiError,
    LomiAuth,
    LomiAuthError,
    LomiClient,
    LomiConnectionError,
    LomiError,
)
from .const import (
    CONF_POLL_INTERVAL,
    CONF_POLL_PENDING,
    CONF_POLL_STATUS,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_PENDING,
    DEFAULT_POLL_STATUS,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)
STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)
STEP_REGISTER_SCHEMA = vol.Schema(
    {
        vol.Required("serial_number"): str,
        vol.Required("nickname", default="Lomi"): str,
    }
)


class LomiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Lomi config flow: email + password -> Cognito -> devices."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._email: str | None = None
        self._password: str | None = None
        self._refresh_token: str | None = None
        self._user_id: str | None = None
        self._client: LomiClient | None = None

    async def _async_login(self, email: str, password: str) -> str | None:
        """Try to log in; return an error key or None on success."""
        session = async_get_clientsession(self.hass)
        auth = LomiAuth(session)
        try:
            await auth.async_login(email, password)
            client = LomiClient(session, auth)
            user = await client.async_get_user()
        except LomiAuthError:
            return "invalid_auth"
        except LomiConnectionError:
            return "cannot_connect"
        except (LomiApiError, LomiError):
            _LOGGER.exception("Unexpected error talking to the Lomi cloud")
            return "unknown"
        self._email, self._password = email, password
        self._refresh_token = auth.refresh_token
        self._user_id = str(user.get("_id") or auth.tokens.claims.get("sub") or email)
        self._client = client
        return None

    def _entry_data(self) -> dict[str, Any]:
        return {
            CONF_EMAIL: self._email,
            CONF_PASSWORD: self._password,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_USER_ID: self._user_id,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial step: account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            error = await self._async_login(email, user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(self._user_id)
                self._abort_if_unique_id_configured()
                assert self._client is not None
                try:
                    devices = await self._client.async_get_devices()
                except LomiError:
                    devices = []
                if not devices:
                    return await self.async_step_register()
                return self.async_create_entry(
                    title=self._email or "Lomi", data=self._entry_data()
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_register(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No devices on the account: offer to register one by serial number."""
        errors: dict[str, str] = {}
        assert self._client is not None
        if user_input is not None:
            serial = user_input["serial_number"].strip().upper()
            nickname = user_input["nickname"].strip() or "Lomi"
            if not serial:
                errors["serial_number"] = "invalid_serial"
            else:
                try:
                    await self._client.async_register_device(serial, nickname)
                except LomiApiError as err:
                    _LOGGER.warning("Device registration rejected: %s", err)
                    errors["base"] = "register_failed"
                except LomiConnectionError:
                    errors["base"] = "cannot_connect"
                except LomiError:
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=self._email or "Lomi", data=self._entry_data()
                    )
        return self.async_show_form(
            step_id="register",
            data_schema=STEP_REGISTER_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials stopped working; ask for the password again."""
        self._email = entry_data.get(CONF_EMAIL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            email = self._email or entry.data[CONF_EMAIL]
            error = await self._async_login(email, user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(self._user_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates=self._entry_data()
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LomiOptionsFlow:
        """Return the options flow."""
        return LomiOptionsFlow()


class LomiOptionsFlow(OptionsFlow):
    """Polling options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_POLL_STATUS,
                    default=options.get(CONF_POLL_STATUS, DEFAULT_POLL_STATUS),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_POLL_PENDING,
                    default=options.get(CONF_POLL_PENDING, DEFAULT_POLL_PENDING),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
