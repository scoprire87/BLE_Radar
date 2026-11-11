"""Flusso di configurazione UI per BLE Fingerprint.

Come in BPS, l'installazione non chiede nulla: mappa, stanze e dispositivi
tracciati si gestiscono dalla pagina "BLE Radar" nella barra laterale. Le
opzioni contengono solo le impostazioni del calcolo della posizione.
"""
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
from homeassistant.helpers import selector

from .const import (
    CONF_CONFIRMATION_SECONDS,
    CONF_MAP_SCALE,
    CONF_MAX_DISTANCE,
    CONF_MIN_COMMON_PROXIES,
    CONF_SHOW_PANEL,
    CONF_SMOOTHING,
    CONF_TOP_K,
    CONF_TRACKERS,
    CONF_TRILATERATION,
    CONF_UNKNOWN_TIMEOUT,
    DEFAULT_CONFIRMATION_SECONDS,
    DEFAULT_MAP_SCALE,
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_COMMON_PROXIES,
    DEFAULT_SHOW_PANEL,
    DEFAULT_SMOOTHING,
    DEFAULT_TOP_K,
    DEFAULT_TRILATERATION,
    DEFAULT_UNKNOWN_TIMEOUT,
    DOMAIN,
    PANEL_TITLE,
    SECTION_ADVANCED,
)

# Opzioni e relativi valori predefiniti
BASIC_DEFAULTS: dict[str, Any] = {
    CONF_SHOW_PANEL: DEFAULT_SHOW_PANEL,
    CONF_CONFIRMATION_SECONDS: DEFAULT_CONFIRMATION_SECONDS,
    CONF_UNKNOWN_TIMEOUT: DEFAULT_UNKNOWN_TIMEOUT,
    CONF_SMOOTHING: DEFAULT_SMOOTHING,
}

ADVANCED_DEFAULTS: dict[str, Any] = {
    CONF_TRILATERATION: DEFAULT_TRILATERATION,
    CONF_MAP_SCALE: DEFAULT_MAP_SCALE,
    CONF_MAX_DISTANCE: DEFAULT_MAX_DISTANCE,
    CONF_TOP_K: DEFAULT_TOP_K,
    CONF_MIN_COMMON_PROXIES: DEFAULT_MIN_COMMON_PROXIES,
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


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SHOW_PANEL): selector.BooleanSelector(),
        vol.Required(CONF_CONFIRMATION_SECONDS): _number(0, 120, 1, "s"),
        vol.Required(CONF_UNKNOWN_TIMEOUT): _number(0, 3600, 1, "s"),
        vol.Required(CONF_SMOOTHING): selector.BooleanSelector(),
        vol.Required(SECTION_ADVANCED): section(
            vol.Schema(
                {
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


class BLEFingerprintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Gestisce il flusso di configurazione UI per BLE Fingerprint."""

    VERSION = 3

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BLEFingerprintOptionsFlow:
        """Restituisce il flusso delle opzioni."""
        return BLEFingerprintOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Un'unica installazione, senza domande: il resto è nella pagina laterale."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is None:
            return self.async_show_form(step_id="user")

        await self.async_set_unique_id(DOMAIN)
        return self.async_create_entry(
            title=PANEL_TITLE, data={}, options={CONF_TRACKERS: []}
        )


class BLEFingerprintOptionsFlow(OptionsFlow):
    """Impostazioni del calcolo della posizione."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gestisce le opzioni."""
        current = dict(self.config_entry.options)

        if user_input is not None:
            advanced = user_input.get(SECTION_ADVANCED) or {}
            # i dispositivi tracciati si gestiscono dalla pagina laterale
            options: dict[str, Any] = {CONF_TRACKERS: current.get(CONF_TRACKERS) or []}
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
            **{key: current.get(key, default) for key, default in BASIC_DEFAULTS.items()},
            SECTION_ADVANCED: {
                key: current.get(key, default)
                for key, default in ADVANCED_DEFAULTS.items()
            },
        }

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, suggested),
        )
