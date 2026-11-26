<img src="custom_components/ble_fingerprint/brand/icon.png" alt="BLE Radar" width="96" align="right">

# BLE Radar

🇬🇧 English · 🇮🇹 [Italiano](README.it.md)

[![Marketplace](https://img.shields.io/badge/Home%20Assistant-Marketplace-41BDF5.svg)](#installation)
[![Validate](https://github.com/scoprire87/BLE_Radar/actions/workflows/validate.yml/badge.svg)](https://github.com/scoprire87/BLE_Radar/actions/workflows/validate.yml)
[![Tests](https://github.com/scoprire87/BLE_Radar/actions/workflows/tests.yml/badge.svg)](https://github.com/scoprire87/BLE_Radar/actions/workflows/tests.yml)

Indoor positioning for Home Assistant. BLE Radar estimates the **room**, the **floor**
and the **coordinates (x, y)** of a phone or tag from the BLE distances measured by your
proxies, for example the `sensor.<device>_distance_to_<proxy>` sensors created by
[Bermuda](https://github.com/agittins/bermuda) with ESPHome or Shelly Bluetooth proxies.

It combines two methods:
- **fingerprinting**: the current distances are compared with reference points recorded
  in each room, which copes well with walls and proxies mounted inside wall boxes;
- **trilateration**: the position and the correction factor of every proxy are
  **estimated automatically** from the recorded points, so you never have to measure
  where your proxies are.

Everything is configured from a **sidebar page**: upload a floor plan, draw the rooms,
pick the devices to track and follow a guided calibration. A **dashboard card** shows
the live position. No extra Python libraries are needed (no numpy/scipy).

![How it works: BLE proxies measure the signal, Bermuda turns it into distances, BLE Radar combines fingerprinting and trilateration and publishes room, floor and coordinates](img/how-it-works.svg)

## Requirements

- Home Assistant 2024.11 or newer.
- Distance sensors for the device to track, typically from
  [Bermuda](https://github.com/agittins/bermuda) (any integration that creates
  `..._distance_to_<proxy>` sensors in metres works).
- At least 3 Bluetooth proxies for trilateration; fingerprinting works with fewer.

## Installation

1. Go to **Settings → Marketplace**, search for **BLE Radar** (or use the button
   below) and select **Install**. Restart Home Assistant if the Marketplace asks to.

   [![Open in the Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=scoprire87&repository=BLE_Radar)

2. Settings → Devices & services → Add integration → *BLE Radar*. It asks nothing: the
   **BLE Radar** page appears in the sidebar.

Not listed? In **Settings → Marketplace** select **Add from link**, enter
`scoprire87/BLE_Radar` and install it (adding links requires connecting GitHub). On
Home Assistant versions before the Marketplace, use HACS → ⋮ → Custom repositories
(category *Integration*). Otherwise, copy
`custom_components/ble_fingerprint` into the `custom_components` folder of the
configuration.

## The BLE Radar page

Everything is set up from the sidebar page (administrators only).

### 1. Devices

Choose the devices to track: devices with "distance to …" sensors are listed, Bermuda
devices first. Each tracked device gets a position sensor. When Bermuda creates a new
distance sensor for the device (for example after adding a proxy), it is picked up
automatically.

**Find Bluetooth devices** helps with devices not yet tracked by Bermuda:

- **Search nearby devices** lists what the proxies hear, closest first: bring the
  device next to a proxy. Each one shows the address (or iBeacon id) to add in
  Bermuda: **Open Bermuda** → *Configure* → *Select Devices*, select it and save.
  After a few seconds it appears in *Add a device*. BLE Radar never changes
  Bermuda's settings itself.
  - **iBeacon**: a fixed identifier, whatever MAC is used. **Android phones**: turn on
    the *BLE Transmitter* sensor of the Home Assistant app (Settings → Companion app →
    Manage sensors) and the phone shows up here, no IRK needed.
  - **Fixed address**: tags, watches, bands, and earbuds that keep their MAC.
    Earbuds are usually visible only while they are *not* connected to the phone
    (case open, or just taken out): while playing audio they use classic Bluetooth,
    which the proxies cannot hear. Search with the earbuds in use to check yours.
- **Phones with a private address** (iPhone, Android without the app transmitter)
  change MAC every few minutes and are only counted. Paste the phone's **IRK**: BLE
  Radar adds it to Private BLE Device, which Bermuda tracks by itself. The IRK can't
  be read over the air; it comes from a device the phone is paired with (for
  example the Keychain of a Mac on the same iCloud account, or an ESP32 enrolment).

### 2. Floor plan & rooms

1. **New floor**: give it a name (e.g. "Ground floor") and upload the floor plan (PNG,
   JPEG or WEBP; large images are downscaled automatically).
2. **Set scale**: click two points whose distance you know (for example both ends of a
   wall) and enter the metres.
3. **Draw room**: click the corners of the room in order, give it a name and save.
   With **Edit shape** you can drag corners, add a corner by clicking near an edge and
   remove one with a double click.
4. **Move proxies**: the proxies with distance sensors are listed; **Place …** and
   click on the floor plan where the proxy is (a filled square). Proxies not placed by
   hand are estimated from the recorded points (dashed squares): drag one to correct
   it, click a placed proxy to go back to the estimate.

The map of the page can be zoomed with **−** / **+** above it (up to 400%), by
pinching with two fingers or with Ctrl + mouse wheel, and moved by dragging it with
a finger or the mouse, to place proxies, corners and calibration points precisely.

Uploading or replacing the floor plan keeps rooms, points and proxies on the same
spot. Maps saved by older versions with rooms drawn on the blank sheet before the
plan was uploaded (small, in the top-left corner) get a **Fit them to the floor
plan** button; recorded points left far outside rooms that were redrawn afterwards
get **Move them back into their rooms** (guided points return to their spot, the
recorded distances are kept).

The map is stored in Home Assistant's `.storage` and the floor plan images in the
`ble_fingerprint` folder of the configuration, outside the integration: updates and
reinstalls keep them.

### 3. Guided calibration

The points recorded with one device are used by **every** tracked device. Phones
transmit with different power, so the same spot gives different distances: to make
another device more precise, record some points with it too (even just a few room
centres). Each point keeps the measurements of every device that recorded it; a device
uses its own where it has them, and elsewhere those of the other devices multiplied by
its **distance factor**, learned from the points recorded by both (shown in the Devices
tab). In the calibration page a full green point was recorded with the selected device,
a green ring only with another one. Points saved by older versions don't store the
device: they are used by every device as they are. **Delete the calibration of …** and
**Delete all calibrations** remove recorded points (rooms and proxies placed by hand
stay), for example to start again choosing the first device.

Choose a room (the table shows which rooms are done): the page shows the points to
record one at a time. The **centre is required**; then there is one **optional** point
per corner of the room, half a metre away from the walls, which improves precision
near the walls and the estimate of the proxy positions (**Skip** moves on). Go to the
highlighted point holding the phone the way you usually carry it, stand still and
press **Record**: the distances are sampled for the chosen duration (15 seconds or
more is recommended with Shelly proxies) and saved with the right coordinates and
floor. The calibration is complete when every room has its centre.

![Guided calibration: the point to reach is highlighted in orange, recorded points are green](img/panel-calibration.png)

### 4. Map

Shows rooms, proxies and the position of the tracked devices in real time.

![Live map with the drawn rooms, the estimated proxies and the position](img/panel-map.png)

## Dashboard card

The **BLE Radar** card shows the floor plan with live positions:

![BLE Radar card on a dashboard](img/card.png)

```yaml
type: custom:ble-radar-card
title: Where we are        # optional
floor: Ground floor        # optional: floor name (default: the first floor)
show_proxies: false        # optional
show_rooms: true           # optional
```

## The position sensor

- **State**: the estimated room, or `unknown`.
- **Attributes**:
  - `x`, `y`: estimated coordinates, in floor plan pixels;
  - `floor`, `floor_name`: floor id and name;
  - `fingerprint_room`: the room suggested by fingerprinting alone;
  - `confidence`: confidence of the estimate, from 0 to 1;
  - `method`: `fingerprint`, `trilateration` or `combined`;
  - `score`: difference in metres from the most similar recorded point (lower is
    better);
  - `status`: `ok`, or why there is no position: `no_sensors` (the device has no
    distance sensors), `no_distances` (no proxy hears it), `no_map` (no recorded
    point), `no_match` (too few proxies in common with the recorded points). The Map
    tab shows it next to the device;
  - `own_points`: points recorded with this device; `distance_factor`: correction
    applied to the points recorded with other devices;
  - `proxies`: proxies currently in range; `trilateration_proxies` and
    `trilateration_rms`: proxies used by trilateration and its residual in metres.

Each position sensor belongs to its own device, linked to the tracked device.

## How it works

1. **Fingerprinting.** For every recorded point, the RMS difference (in metres) between
   the current and the recorded distances is computed. A proxy seen on one side only
   counts as being at the maximum distance. Among the *k* most similar points, the
   **room** is chosen by a weighted vote and the coordinates are their weighted average.
   The room also gives the **floor**.
2. **Proxies estimated from the map.** From the points with coordinates, the position
   of every proxy and its **correction factor** (measured / real distance) are fitted
   for each floor. The fit uses an asymmetric loss, because walls can only make
   distances longer. The measurements come from your own phone, so the correction is
   calibrated for the tracked device.
3. **Trilateration.** With at least 3 estimated proxies, the position is a weighted
   least-squares fit of the corrected distances (1/r² weights, so nearby proxies count
   more), starting from the fingerprint estimate and bounded to the map. Sudden
   distance jumps are down-weighted instead of discarded.
4. **Fusion.** Both estimates are averaged according to their quality (residual and
   number of proxies).
5. **Smoothing.** A constant-velocity Kalman filter removes jitter; it resets after an
   absence or a floor change.
6. **Drawn rooms.** If the floor has drawn rooms, the room is the one containing the
   computed position; a position outside every room (for example inside a wall) is
   snapped into the nearest room.
7. The room only changes when the new one stays the best match for the confirmation
   time; without any valid proxy for a while, the position becomes unknown.

## Options

From *Configure* on the integration:

| Option | Default | Description |
|---|---|---|
| Show the sidebar page | on | The BLE Radar page |
| Room change confirmation | 5 s | How long the new room must stay the best match (0 = immediate) |
| Back to unknown | 60 s | Time without any valid proxy (0 = never) |
| Smooth movement | on | Kalman filter on the coordinates |

In the **Advanced** section:

| Option | Default | Description |
|---|---|---|
| Trilateration | on | Use the proxies estimated from the map to refine the coordinates |
| Default scale | 1 | Units per metre for points without a floor or floors without a scale (floor scales are set in the sidebar page) |
| Maximum distance | 10 m | Longer distances are ignored |
| Nearest points (k) | 3 | Points used for room and coordinates |
| Minimum common proxies | 2 | Proxies a reading and a point must share |

## Services

Useful for automations or YAML fans; the sidebar page calls them for you.

| Service | What it does |
|---|---|
| `record_vertex` | Record a point from the current distances (median over `duration` seconds) |
| `save_vertex` | Save a point with distances given by hand (`distances: {sensor: metres}`) |
| `delete_vertex` | Delete a point (`room`, `vertex_id`) |
| `delete_room` | Delete a room with all its points |
| `set_proxy` | Set a proxy position (and optionally its correction) by hand; without `x`/`y` the manual setting is removed |
| `get_map` | Return the map, the manually placed proxies and the **estimated** ones (position, correction factor, residual) |

The map is stored in `.storage/ble_fingerprint_map`, floor plans in the
`ble_fingerprint` folder of the Home Assistant configuration.

## Tips

- Record several points per room (corners, centre, places where you usually stay),
  holding the phone the way you usually carry it.
- On Android, in the Home Assistant Companion app, set the *BLE Transmitter* sensor to
  high transmit power and low-latency advertising for steadier distances.
- If the room flickers, increase the room change confirmation time.

## Known limitations

- A proxy that is switched off counts as "far away": until it comes back, rooms near
  it are penalised.
- Accuracy depends on the number and quality of the recorded points.
- Trilateration needs points with coordinates; a proxy outside the area of the
  recorded points (for example in the garden) is estimated less accurately: you can
  place it by hand.

## Upgrading from 1.x

The existing configuration is migrated automatically: the tracked device, its name and
the entity stay the same, and the saved map remains valid (1.0 installations, which did
not store the device, ask you to choose it in the sidebar page). Points recorded
without a floor keep working with fingerprinting only; to use drawn rooms, create the
floor in the sidebar page and record the points with the guided calibration.

## Credits

The trilateration engine, the quality score, the Kalman filter and the per-proxy
correction are inspired by [BPS – BLE Positioning System](https://github.com/Hogster/BPS)
by Hogster (MIT licence, see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). Distance
sensors are usually provided by [Bermuda](https://github.com/agittins/bermuda).

## Development

```bash
pip install -r requirements_test.txt
pytest
```
