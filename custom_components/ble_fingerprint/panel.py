"""Pagina nella barra laterale e card per la dashboard."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .const import (
    CONF_SHOW_PANEL,
    DEFAULT_SHOW_PANEL,
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    STATIC_URL,
)

_LOGGER = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"
PANEL_COMPONENT = "ble-radar-panel"


async def async_setup_frontend(hass: HomeAssistant) -> None:
    """Pubblica i file del pannello e registra la card per le dashboard."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("frontend_version") or getattr(hass, "http", None) is None:
        return

    # pylint: disable-next=import-outside-toplevel
    from homeassistant.components.http import StaticPathConfig

    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version or "0")
    domain_data["frontend_version"] = version

    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), cache_headers=False)]
    )

    if "frontend" in hass.config.components:
        # pylint: disable-next=import-outside-toplevel
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, f"{STATIC_URL}/ble-radar-card.js?v={version}")


@callback
def async_register_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Aggiunge la pagina "BLE Radar" alla barra laterale (se abilitata)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    version = domain_data.get("frontend_version")
    if (
        not version
        or "frontend" not in hass.config.components
        or not entry.options.get(CONF_SHOW_PANEL, DEFAULT_SHOW_PANEL)
        or domain_data.get("panel_entry")
    ):
        return

    # pylint: disable-next=import-outside-toplevel
    from homeassistant.components.frontend import async_register_built_in_panel

    async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        require_admin=True,
        config={
            "_panel_custom": {
                "name": PANEL_COMPONENT,
                "module_url": f"{STATIC_URL}/{PANEL_COMPONENT}.js?v={version}",
                "embed_iframe": False,
                "trust_external": False,
            }
        },
    )
    domain_data["panel_entry"] = entry.entry_id


@callback
def async_remove_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Toglie la pagina dalla barra laterale quando l'integrazione si scarica."""
    domain_data = hass.data.get(DOMAIN, {})
    if domain_data.get("panel_entry") != entry.entry_id:
        return

    # pylint: disable-next=import-outside-toplevel
    from homeassistant.components.frontend import async_remove_panel as remove

    remove(hass, PANEL_URL_PATH)
    domain_data.pop("panel_entry", None)
