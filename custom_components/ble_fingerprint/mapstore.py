"""Archivio della mappa: piani, planimetrie, stanze, vertici e proxy.

Struttura salvata in `.storage/ble_fingerprint_map`:

    {
      "floors":  {floor_id: {"name", "image", "width", "height", "scale"}},
      "zones":   {stanza: {"floor": floor_id, "polygon": [[x, y], ...]}},
      "rooms":   {stanza: {vertex_id: {"distances", "coords": {x, y}, "floor"}}},
      "proxies": {proxy: {"x", "y", "floor", "factor"?}}   # posizioni manuali
    }

Le coordinate sono in pixel della planimetria del piano; `scale` è in pixel
per metro. Le planimetrie sono salvate in `<config>/ble_fingerprint/`.
"""
from __future__ import annotations

import base64
import binascii
import copy
import logging
from pathlib import Path
import secrets
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SIGNAL_MAP_UPDATED, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

IMAGE_DIR = "ble_fingerprint"
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class MapStore:
    """Mappa condivisa tra sensori, servizi e pannello."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = {}
        # i sensori ricalcolano piani, proxy e stanze quando la versione cambia
        self.version = 0

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not isinstance(data, dict):
            data = {}
        for key in ("floors", "zones", "rooms", "proxies"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        self.data = data
        self.version = 1

    # --- accesso ---------------------------------------------------------

    @property
    def floors(self) -> dict[str, dict[str, Any]]:
        return self.data["floors"]

    @property
    def zones(self) -> dict[str, dict[str, Any]]:
        return self.data["zones"]

    @property
    def rooms(self) -> dict[str, dict[str, Any]]:
        return self.data["rooms"]

    @property
    def proxies(self) -> dict[str, dict[str, Any]]:
        return self.data["proxies"]

    def floor_scale(self, floor_id: str | None) -> float | None:
        """Pixel per metro del piano (None se non calibrato)."""
        floor = self.floors.get(floor_id) if floor_id else None
        scale = floor.get("scale") if floor else None
        return float(scale) if isinstance(scale, (int, float)) and scale > 0 else None

    def snapshot(self) -> dict[str, Any]:
        """Copia della mappa per il pannello e il servizio get_map."""
        return copy.deepcopy(self.data)

    # --- salvataggio -----------------------------------------------------

    async def async_save(self) -> None:
        await self._store.async_save(self.data)
        self.version += 1
        async_dispatcher_send(self.hass, SIGNAL_MAP_UPDATED)

    # --- piani -----------------------------------------------------------

    def new_floor_id(self) -> str:
        while (floor_id := secrets.token_hex(4)) in self.floors:
            pass
        return floor_id

    def _image_path(self, filename: str) -> Path:
        return Path(self.hass.config.path(IMAGE_DIR, filename))

    async def async_set_floor_image(self, floor_id: str, data_url: str) -> str:
        """Salva la planimetria (data URL base64) e restituisce il nome del file."""
        try:
            header, encoded = data_url.split(",", 1)
            mime = header.split(":", 1)[1].split(";", 1)[0]
            raw = base64.b64decode(encoded, validate=True)
        except (IndexError, ValueError, binascii.Error) as err:
            raise ValueError("Invalid image") from err
        if mime not in IMAGE_TYPES:
            raise ValueError("Unsupported format: use PNG, JPEG or WEBP")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Image too large (maximum 8 MB)")

        filename = f"{floor_id}.{IMAGE_TYPES[mime]}"
        old = self.floors.get(floor_id, {}).get("image")

        def _write() -> None:
            path = self._image_path(filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            if old and old != filename:
                self._image_path(old).unlink(missing_ok=True)

        await self.hass.async_add_executor_job(_write)
        return filename

    async def async_get_floor_image(self, floor_id: str) -> str | None:
        """Planimetria del piano come data URL (None se assente)."""
        filename = self.floors.get(floor_id, {}).get("image")
        if not filename:
            return None
        ext = filename.rsplit(".", 1)[-1]
        mime = next((m for m, e in IMAGE_TYPES.items() if e == ext), "image/png")

        def _read() -> bytes | None:
            path = self._image_path(filename)
            return path.read_bytes() if path.is_file() else None

        raw = await self.hass.async_add_executor_job(_read)
        if raw is None:
            return None
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"

    async def async_delete_floor(self, floor_id: str) -> None:
        """Elimina un piano con stanze, vertici e proxy manuali."""
        floor = self.floors.pop(floor_id, None)
        for name in [n for n, z in self.zones.items() if z.get("floor") == floor_id]:
            del self.zones[name]
        for room in list(self.rooms):
            vertices = self.rooms[room]
            for vertex_id in [v for v, d in vertices.items() if d.get("floor") == floor_id]:
                del vertices[vertex_id]
            if not vertices:
                del self.rooms[room]
        for proxy in [p for p, d in self.proxies.items() if d.get("floor") == floor_id]:
            del self.proxies[proxy]

        if floor and (filename := floor.get("image")):
            await self.hass.async_add_executor_job(
                lambda: self._image_path(filename).unlink(missing_ok=True)
            )

    # --- stanze ----------------------------------------------------------

    def rename_room(self, old: str, new: str) -> None:
        """Rinomina una stanza (poligono e vertici)."""
        if old == new:
            return
        if new in self.zones or new in self.rooms:
            raise ValueError(f"A room called {new} already exists")
        if old in self.zones:
            self.zones[new] = self.zones.pop(old)
        if old in self.rooms:
            self.rooms[new] = self.rooms.pop(old)

    def delete_room(self, name: str) -> bool:
        """Elimina poligono e vertici di una stanza."""
        found = self.zones.pop(name, None) is not None
        found = self.rooms.pop(name, None) is not None or found
        return found


def get_map_store(hass: HomeAssistant) -> MapStore:
    """MapStore condiviso dell'integrazione."""
    return hass.data[DOMAIN]["map"]
