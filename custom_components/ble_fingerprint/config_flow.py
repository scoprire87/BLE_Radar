"""Flusso di configurazione UI per BLE Fingerprint."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CONFIRMATION_SECONDS,
    CONF_DEVICE,
    CONF_MAX_DISTANCE,
    CONF_MIN_COMMON_PROXIES,
    CONF_NAME,
    CONF_SENSORS,
    CONF_TOP_K,
    CONF_UNKNOWN_TIMEOUT,
    DEFAULT_CONFIRMATION_SECONDS,
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_COMMON_PROXIES,
    DEFAULT_NAME,
    DEFAULT_TOP_K,
    DEFAULT_UNKNOWN_TIMEOUT,
    DOMAIN,
)
from .sensor import async_resolve_proxy_sensors

_SOURCE_FIELDS: dict[Any, Any] = {
    vol.Optional(CONF_DEVICE): selector.DeviceSelector(),
    vol.Optional(CONF_SENSORS): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", multiple=True)
    ),
}

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): selector.TextSelector(),
        **_SOURCE_FIELDS,
    }
)


def _number(minimum: float, maximum: float, step: float, unit: str | None = None):
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


OPTIONS_SCHEMA = vol.Schema(
    {
        **_SOURCE_FIELDS,
        vol.Required(CONF_CONFIRMATION_SECONDS): _number(0, 120, 1, "s"),
        vol.Required(CONF_UNKNOWN_TIMEOUT): _number(0, 3600, 1, "s"),
        vol.Required(CONF_MAX_DISTANCE): _number(1, 100, 0.5, "m"),
        vol.Required(CONF_TOP_K): _number(1, 10, 1),
        vol.Required(CONF_MIN_COMMON_PROXIES): _number(1, 10, 1),
    }
)


class BLEFingerprintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Gestisce il flusso di configurazione UI per BLE Fingerprint."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BLEFingerprintOptionsFlow:
        """Restituisce il flusso delle opzioni."""
        return BLEFingerprintOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Chiede nome e sensori di distanza del dispositivo da tracciare."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            device_id = user_input.get(CONF_DEVICE)
            sensors = user_input.get(CONF_SENSORS) or []

            if not name:
                errors[CONF_NAME] = "name_required"
            elif not async_resolve_proxy_sensors(self.hass, device_id, sensors):
                errors["base"] = "no_sensors"
            else:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_NAME: name,
                        CONF_DEVICE: device_id,
                        CONF_SENSORS: sensors,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )


class BLEFingerprintOptionsFlow(OptionsFlow):
    """Modifica sensori e soglie di un sensore di posizione."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gestisce le opzioni."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            device_id = user_input.get(CONF_DEVICE)
            sensors = user_input.get(CONF_SENSORS) or []

            if not async_resolve_proxy_sensors(self.hass, device_id, sensors):
                errors["base"] = "no_sensors"
            else:
                return self.async_create_entry(
                    data={
                        # sempre presenti, così svuotare un campo ha effetto
                        CONF_DEVICE: device_id,
                        CONF_SENSORS: sensors,
                        CONF_CONFIRMATION_SECONDS: int(
                            user_input[CONF_CONFIRMATION_SECONDS]
                        ),
                        CONF_UNKNOWN_TIMEOUT: int(user_input[CONF_UNKNOWN_TIMEOUT]),
                        CONF_MAX_DISTANCE: float(user_input[CONF_MAX_DISTANCE]),
                        CONF_TOP_K: int(user_input[CONF_TOP_K]),
                        CONF_MIN_COMMON_PROXIES: int(
                            user_input[CONF_MIN_COMMON_PROXIES]
                        ),
                    }
                )

        suggested = {
            CONF_DEVICE: current.get(CONF_DEVICE),
            CONF_SENSORS: current.get(CONF_SENSORS) or [],
            CONF_CONFIRMATION_SECONDS: current.get(
                CONF_CONFIRMATION_SECONDS, DEFAULT_CONFIRMATION_SECONDS
            ),
            CONF_UNKNOWN_TIMEOUT: current.get(
                CONF_UNKNOWN_TIMEOUT, DEFAULT_UNKNOWN_TIMEOUT
            ),
            CONF_MAX_DISTANCE: current.get(CONF_MAX_DISTANCE, DEFAULT_MAX_DISTANCE),
            CONF_TOP_K: current.get(CONF_TOP_K, DEFAULT_TOP_K),
            CONF_MIN_COMMON_PROXIES: current.get(
                CONF_MIN_COMMON_PROXIES, DEFAULT_MIN_COMMON_PROXIES
            ),
            **(user_input or {}),
        }

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, suggested),
            errors=errors,
        )
