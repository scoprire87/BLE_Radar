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
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr, selector

from .const import (
    CONF_CONFIRMATION_SECONDS,
    CONF_DEVICE,
    CONF_MAP_SCALE,
    CONF_MAX_DISTANCE,
    CONF_MIN_COMMON_PROXIES,
    CONF_NAME,
    CONF_SENSORS,
    CONF_SMOOTHING,
    CONF_TOP_K,
    CONF_TRILATERATION,
    CONF_UNKNOWN_TIMEOUT,
    DEFAULT_CONFIRMATION_SECONDS,
    DEFAULT_MAP_SCALE,
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_COMMON_PROXIES,
    DEFAULT_NAME,
    DEFAULT_SMOOTHING,
    DEFAULT_TOP_K,
    DEFAULT_TRILATERATION,
    DEFAULT_UNKNOWN_TIMEOUT,
    DOMAIN,
    SECTION_ADVANCED,
)
from .sensor import async_resolve_proxy_sensors

# Opzioni avanzate e relativi valori predefiniti (sezione comprimibile)
ADVANCED_DEFAULTS: dict[str, Any] = {
    CONF_TRILATERATION: DEFAULT_TRILATERATION,
    CONF_MAP_SCALE: DEFAULT_MAP_SCALE,
    CONF_MAX_DISTANCE: DEFAULT_MAX_DISTANCE,
    CONF_TOP_K: DEFAULT_TOP_K,
    CONF_MIN_COMMON_PROXIES: DEFAULT_MIN_COMMON_PROXIES,
}

BASIC_DEFAULTS: dict[str, Any] = {
    CONF_CONFIRMATION_SECONDS: DEFAULT_CONFIRMATION_SECONDS,
    CONF_UNKNOWN_TIMEOUT: DEFAULT_UNKNOWN_TIMEOUT,
    CONF_SMOOTHING: DEFAULT_SMOOTHING,
}

_INT_OPTIONS = {
    CONF_CONFIRMATION_SECONDS,
    CONF_UNKNOWN_TIMEOUT,
    CONF_TOP_K,
    CONF_MIN_COMMON_PROXIES,
}


def _number(minimum: float, maximum: float, step: float, unit: str | None = None):
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


BERMUDA_DOMAIN = "bermuda"

_SENSORS = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", multiple=True)
)

def _device_selector(hass) -> selector.DeviceSelector:
    """Selettore del dispositivo: se Bermuda è installato, solo i suoi dispositivi.

    Così l'elenco mostra soltanto i dispositivi effettivamente tracciati via BLE.
    Senza Bermuda (sensori di distanza da altre integrazioni) mostra tutti i
    dispositivi.
    """
    if hass.config_entries.async_entries(BERMUDA_DOMAIN):
        return selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration=BERMUDA_DOMAIN)
        )
    return selector.DeviceSelector()


def _user_schema(hass) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_DEVICE): _device_selector(hass),
            vol.Optional(CONF_NAME): selector.TextSelector(),
            vol.Required(SECTION_ADVANCED): section(
                vol.Schema({vol.Optional(CONF_SENSORS): _SENSORS}),
                {"collapsed": True},
            ),
        }
    )


def _options_schema(hass) -> vol.Schema:
    # il dispositivo è il primo campo del modulo
    return vol.Schema(
        {vol.Optional(CONF_DEVICE): _device_selector(hass), **OPTIONS_SCHEMA.schema}
    )


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONFIRMATION_SECONDS): _number(0, 120, 1, "s"),
        vol.Required(CONF_UNKNOWN_TIMEOUT): _number(0, 3600, 1, "s"),
        vol.Required(CONF_SMOOTHING): selector.BooleanSelector(),
        vol.Required(SECTION_ADVANCED): section(
            vol.Schema(
                {
                    vol.Optional(CONF_SENSORS): _SENSORS,
                    vol.Required(CONF_TRILATERATION): selector.BooleanSelector(),
                    vol.Required(CONF_MAP_SCALE): _number(0.01, 10000, 0.01),
                    vol.Required(CONF_MAX_DISTANCE): _number(1, 100, 0.5, "m"),
                    vol.Required(CONF_TOP_K): _number(1, 10, 1),
                    vol.Required(CONF_MIN_COMMON_PROXIES): _number(1, 10, 1),
                }
            ),
            {"collapsed": True},
        ),
    }
)


def _device_name(hass, device_id: str | None) -> str | None:
    if device_id and (device := dr.async_get(hass).async_get(device_id)):
        return device.name_by_user or device.name
    return None


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
        """Basta scegliere il dispositivo da tracciare."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input.get(CONF_DEVICE)
            sensors = (user_input.get(SECTION_ADVANCED) or {}).get(CONF_SENSORS) or []
            name = (user_input.get(CONF_NAME) or "").strip()
            if not name:
                device_name = _device_name(self.hass, device_id)
                name = f"Posizione {device_name}" if device_name else DEFAULT_NAME

            if not async_resolve_proxy_sensors(self.hass, device_id, sensors):
                errors["base"] = "no_sensors"
            else:
                if device_id:
                    await self.async_set_unique_id(device_id)
                    self._abort_if_unique_id_configured()
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
            data_schema=self.add_suggested_values_to_schema(
                _user_schema(self.hass), user_input
            ),
            errors=errors,
        )


class BLEFingerprintOptionsFlow(OptionsFlow):
    """Modifica dispositivo, sensori e soglie di un sensore di posizione."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gestisce le opzioni."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            advanced = user_input.get(SECTION_ADVANCED) or {}
            device_id = user_input.get(CONF_DEVICE)
            sensors = advanced.get(CONF_SENSORS) or []

            if not async_resolve_proxy_sensors(self.hass, device_id, sensors):
                errors["base"] = "no_sensors"
            else:
                options: dict[str, Any] = {
                    # sempre presenti, così svuotare un campo ha effetto
                    CONF_DEVICE: device_id,
                    CONF_SENSORS: sensors,
                }
                for key, default in {**BASIC_DEFAULTS, **ADVANCED_DEFAULTS}.items():
                    value = user_input.get(key, advanced.get(key, default))
                    if isinstance(default, bool):
                        options[key] = bool(value)
                    elif key in _INT_OPTIONS:
                        options[key] = int(value)
                    else:
                        options[key] = float(value)
                return self.async_create_entry(data=options)

        suggested: dict[str, Any] = {
            CONF_DEVICE: current.get(CONF_DEVICE),
            **{key: current.get(key, default) for key, default in BASIC_DEFAULTS.items()},
            SECTION_ADVANCED: {
                CONF_SENSORS: current.get(CONF_SENSORS) or [],
                **{
                    key: current.get(key, default)
                    for key, default in ADVANCED_DEFAULTS.items()
                },
            },
        }
        if user_input is not None:
            suggested.update(user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(self.hass), suggested
            ),
            errors=errors,
        )
