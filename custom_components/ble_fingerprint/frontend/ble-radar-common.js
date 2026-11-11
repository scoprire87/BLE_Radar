// Code shared by the BLE Radar sidebar page and dashboard card.
import { translator } from "./ble-radar-i18n.js";

export const SVG_NS = "http://www.w3.org/2000/svg";
export const BLANK_SIZE = { width: 1000, height: 700 };

const ZONE_COLORS = [
  "#42a5f5", "#66bb6a", "#ffa726", "#ab47bc", "#26c6da",
  "#ef5350", "#d4e157", "#8d6e63", "#5c6bc0", "#ec407a",
];
const TRACKER_COLORS = ["#e53935", "#1e88e5", "#43a047", "#fb8c00", "#8e24aa", "#00acc1"];

export function zoneColor(index) {
  return ZONE_COLORS[index % ZONE_COLORS.length];
}

export function trackerColor(index) {
  return TRACKER_COLORS[index % TRACKER_COLORS.length];
}

export function svgEl(tag, attrs = {}, parent = null) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null) el.setAttribute(key, value);
  }
  if (parent) parent.appendChild(el);
  return el;
}

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export async function loadConfig(hass) {
  return hass.connection.sendMessagePromise({ type: "ble_fingerprint/config" });
}

const imageCache = new Map();

export async function loadImage(hass, floorId, { refresh = false } = {}) {
  if (!floorId) return null;
  if (!refresh && imageCache.has(floorId)) return imageCache.get(floorId);
  const result = await hass.connection.sendMessagePromise({
    type: "ble_fingerprint/floor_image",
    floor_id: floorId,
  });
  imageCache.set(floorId, result.image);
  return result.image;
}

export function forgetImage(floorId) {
  imageCache.delete(floorId);
}

// Floor plan size (or a blank sheet when there is no image)
export function floorSize(floor) {
  if (floor && floor.width && floor.height) {
    return { width: floor.width, height: floor.height };
  }
  return { ...BLANK_SIZE };
}

// Marker radius proportional to the map
export function markerSize(size) {
  return Math.max(6, Math.round(Math.min(size.width, size.height) / 70));
}

export function centroid(points) {
  let area = 0, cx = 0, cy = 0;
  for (let i = 0; i < points.length; i++) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    const cross = x1 * y2 - x2 * y1;
    area += cross; cx += (x1 + x2) * cross; cy += (y1 + y2) * cross;
  }
  if (Math.abs(area) < 1e-9) {
    const n = points.length || 1;
    return [points.reduce((s, p) => s + p[0], 0) / n, points.reduce((s, p) => s + p[1], 0) / n];
  }
  area /= 2;
  return [cx / (6 * area), cy / (6 * area)];
}

// Rooms of a floor, with a stable colour per name
export function floorZones(config, floorId) {
  const names = Object.keys(config.zones || {}).sort();
  return names
    .map((name, index) => ({ name, color: zoneColor(index), ...config.zones[name] }))
    .filter((zone) => zone.floor === floorId);
}

// Proxies of a floor: manual position if set, otherwise the estimate
export function floorProxies(config, floorId) {
  const result = {};
  const estimated = (config.estimated_proxies || {})[floorId] || {};
  for (const [name, data] of Object.entries(estimated)) {
    result[name] = { name, x: data.x, y: data.y, manual: data.source === "manual", estimate: data };
  }
  for (const [name, data] of Object.entries(config.proxies || {})) {
    if ((data.floor || null) !== floorId) continue;
    result[name] = { ...(result[name] || {}), name, x: data.x, y: data.y, manual: true };
  }
  return Object.values(result);
}

export function trackerStates(hass, config) {
  return (config.trackers || [])
    .map((tracker, index) => {
      const state = tracker.entity_id ? hass.states[tracker.entity_id] : undefined;
      return { ...tracker, color: trackerColor(index), state };
    });
}

// Draw rooms, proxies and people in an <svg> sized like the map
export function drawMap(svg, { hass, config, floorId, size, layers = {} }) {
  // markers and labels keep a constant on-screen size (~9 px), whatever the
  // floor plan resolution or the card width
  const t = translator(hass);
  const shown = svg.getBoundingClientRect().width;
  const r = shown > 0 ? (9 * size.width) / shown : markerSize(size);
  const show = { zones: true, vertices: false, proxies: true, trackers: true, ...layers };

  if (show.zones) {
    const group = svgEl("g", { class: "zones" }, svg);
    for (const zone of floorZones(config, floorId)) {
      const points = zone.polygon.map((p) => p.join(",")).join(" ");
      svgEl("polygon", {
        points,
        fill: zone.color,
        "fill-opacity": "0.18",
        stroke: zone.color,
        "stroke-width": Math.max(2, r / 3),
        "data-zone": zone.name,
      }, group);
      const [cx, cy] = centroid(zone.polygon);
      // above the centre, so the central calibration point stays visible
      const label = svgEl("text", {
        x: cx, y: cy - r * 2.2,
        "text-anchor": "middle",
        "dominant-baseline": "middle",
        "font-size": r * 1.8,
        "font-weight": "600",
        fill: "var(--primary-text-color, #222)",
        "paint-order": "stroke",
        stroke: "var(--card-background-color, #fff)",
        "stroke-width": r / 2,
        "pointer-events": "none",
      }, group);
      label.textContent = zone.name;
    }
  }

  if (show.vertices) {
    const group = svgEl("g", { class: "vertices" }, svg);
    for (const [room, vertices] of Object.entries(config.rooms || {})) {
      for (const [id, vertex] of Object.entries(vertices)) {
        const c = vertex.coords || {};
        if (vertex.floor !== floorId || c.x == null || c.y == null) continue;
        const dot = svgEl("circle", {
          cx: c.x, cy: c.y, r: r * 0.45,
          fill: "#2e7d32", stroke: "#fff", "stroke-width": r / 6,
        }, group);
        svgEl("title", {}, dot).textContent = `${room} · ${id} (${vertex.proxies} proxy)`;
      }
    }
  }

  if (show.proxies) {
    const group = svgEl("g", { class: "proxies" }, svg);
    for (const proxy of floorProxies(config, floorId)) {
      const g = svgEl("g", { "data-proxy": proxy.name, transform: `translate(${proxy.x},${proxy.y})` }, group);
      svgEl("rect", {
        x: -r * 0.7, y: -r * 0.7, width: r * 1.4, height: r * 1.4,
        rx: r * 0.25,
        fill: proxy.manual ? "#0277bd" : "var(--card-background-color, #fff)",
        stroke: "#0277bd",
        "stroke-width": r / 4,
        "stroke-dasharray": proxy.manual ? null : `${r / 3} ${r / 4}`,
      }, g);
      if (show.proxyLabels) {
        const label = svgEl("text", {
          y: -r * 1.2, "text-anchor": "middle", "font-size": r * 1.2,
          fill: "#0277bd", "paint-order": "stroke",
          stroke: "var(--card-background-color, #fff)", "stroke-width": r / 3,
        }, g);
        label.textContent = proxy.name;
      }
      const info = proxy.estimate
        ? t("proxy.estimated", { factor: proxy.estimate.factor, samples: proxy.estimate.samples })
        : "";
      svgEl("title", {}, g).textContent = `${proxy.name}${proxy.manual ? t("proxy.manual") : info}`;
    }
  }

  if (show.trackers) {
    const group = svgEl("g", { class: "trackers" }, svg);
    for (const tracker of trackerStates(hass, config)) {
      const attrs = tracker.state?.attributes || {};
      if (attrs.floor !== floorId || attrs.x == null || attrs.y == null) continue;
      const g = svgEl("g", { transform: `translate(${attrs.x},${attrs.y})` }, group);
      svgEl("circle", { r: r * 1.6, fill: tracker.color, "fill-opacity": "0.2" }, g);
      svgEl("circle", {
        r: r * 0.9, fill: tracker.color, stroke: "#fff", "stroke-width": r / 4,
      }, g);
      const label = svgEl("text", {
        y: -r * 2, "text-anchor": "middle", "font-size": r * 1.4, "font-weight": "600",
        fill: tracker.color, "paint-order": "stroke",
        stroke: "var(--card-background-color, #fff)", "stroke-width": r / 2.5,
      }, g);
      label.textContent = tracker.name;
      svgEl("title", {}, g).textContent =
        t("tracker.title", { name: tracker.name, state: tracker.state.state, confidence: attrs.confidence ?? "-" });
    }
  }
}

export const BASE_STYLE = `
  :host { display: block; }
  .map {
    position: relative; width: 100%; line-height: 0;
    background: var(--secondary-background-color, #f5f5f5);
    border-radius: 8px; overflow: hidden;
  }
  .map img { width: 100%; height: auto; display: block; user-select: none; }
  .map .blank { width: 100%; display: block; }
  .map svg { position: absolute; inset: 0; width: 100%; height: 100%; }
  .empty { padding: 24px; text-align: center; color: var(--secondary-text-color); line-height: 1.5; }
`;
