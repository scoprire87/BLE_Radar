// Dashboard card: floor plan with live positions.
//
// type: custom:ble-radar-card
// floor: Ground floor       # optional: floor name or id (default: the first one)
// title: Home               # optional
// show_proxies: false       # optional
// show_rooms: true          # optional
//
// The rest of the code is loaded lazily: the element is registered right away,
// so Home Assistant does not show "Configuration error" when the dashboard
// opens before the modules have been downloaded.
let common = null;
let i18n = null;
const commonReady = Promise.all([import("./ble-radar-common.js"), import("./ble-radar-i18n.js")])
  .then(([module, translations]) => { common = module; i18n = translations; });

const CONFIG_REFRESH_MS = 5 * 60 * 1000;

class BleRadarCard extends HTMLElement {
  static getStubConfig() {
    return { title: "BLE Radar" };
  }

  setConfig(config) {
    this._cardConfig = { show_proxies: false, show_rooms: true, ...config };
    this._loadedAt = 0;
    if (this._hass) this._load();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || Date.now() - this._loadedAt > CONFIG_REFRESH_MS) this._load();
    else this._draw();
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  async _load() {
    if (this._loading) return;
    this._loading = true;
    this._loadedAt = Date.now();
    try {
      await commonReady;
      const { loadConfig, loadImage } = common;
      this._config = await loadConfig(this._hass);
      const floors = this._config.floors || {};
      const wanted = this._cardConfig.floor;
      this._floorId = Object.keys(floors).find((id) => id === wanted || floors[id].name === wanted)
        || Object.keys(floors)[0] || null;
      this._image = await loadImage(this._hass, this._floorId).catch(() => null);
      this._error = null;
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._loading = false;
    }
    this._render();
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (!common) return;
    const { BASE_STYLE, escapeHtml, floorSize } = common;
    const t = i18n.translator(this._hass);
    const title = this._cardConfig.title;
    let body;
    if (this._error) {
      body = `<div class="empty">${t("card.unavailable", { error: escapeHtml(this._error) })}</div>`;
    } else if (!this._config) {
      body = `<div class="empty">${t("loading")}</div>`;
    } else if (!this._floorId) {
      body = `<div class="empty">${t("card.no_floor")}</div>`;
    } else {
      const { width, height } = floorSize(this._config.floors[this._floorId]);
      const base = this._image
        ? `<img src="${this._image}" alt="" draggable="false">`
        : `<svg class="blank" viewBox="0 0 ${width} ${height}"><rect width="${width}" height="${height}" fill="var(--card-background-color, #fff)"/></svg>`;
      body = `<div class="map">${base}<svg id="overlay" viewBox="0 0 ${width} ${height}"></svg></div>
        <div id="legend" class="legend"></div>`;
    }
    this.shadowRoot.innerHTML = `
      <style>${BASE_STYLE}${CARD_STYLE}</style>
      <ha-card>
        ${title ? `<h1 class="card-header">${escapeHtml(title)}</h1>` : ""}
        <div class="card-content">${body}</div>
      </ha-card>`;
    // redraw when the card is resized (markers stay readable)
    this._resizeObserver?.disconnect();
    const map = this.shadowRoot.querySelector(".map");
    if (map && window.ResizeObserver) {
      this._resizeObserver = new ResizeObserver(() => this._draw());
      this._resizeObserver.observe(map);
    }
    this._draw();
  }

  disconnectedCallback() {
    this._resizeObserver?.disconnect();
    this._resizeObserver = null;
  }

  connectedCallback() {
    if (this._config && this.shadowRoot && !this._resizeObserver) this._render();
  }

  _draw() {
    const svg = this.shadowRoot?.getElementById("overlay");
    if (!svg || !this._config || !common) return;
    const { drawMap, escapeHtml, floorSize, trackerStates } = common;
    svg.innerHTML = "";
    const size = floorSize(this._config.floors[this._floorId]);
    drawMap(svg, {
      hass: this._hass,
      config: this._config,
      floorId: this._floorId,
      size,
      layers: { zones: this._cardConfig.show_rooms, proxies: this._cardConfig.show_proxies },
    });
    const legend = this.shadowRoot.getElementById("legend");
    if (legend) {
      legend.innerHTML = trackerStates(this._hass, this._config).map((t) => {
        const a = t.state?.attributes || {};
        const where = t.state ? `${t.state.state}${a.floor && a.floor !== this._floorId ? ` (${a.floor_name})` : ""}` : "—";
        return `<span><span class="dot" style="background:${t.color}"></span>${escapeHtml(t.name)}: <b>${escapeHtml(where)}</b></span>`;
      }).join("");
    }
  }
}

const CARD_STYLE = `
  .card-content { padding: 0 16px 16px; }
  .card-header { margin: 0; padding: 16px; font-size: 24px; font-weight: 400; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; font-size: 14px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
`;

// While starting, the Home Assistant frontend can replace the custom element
// registry after this module has run: the card would then be reported as
// missing. For a few seconds, check again and re-register it if needed.
function defineCard() {
  if (window.customElements.get("ble-radar-card")) return true;
  try {
    window.customElements.define("ble-radar-card", class extends BleRadarCard {});
  } catch (_err) {
    return false;
  }
  return true;
}

defineCard();
let checks = 0;
const timer = setInterval(() => {
  defineCard();
  checks += 1;
  if (checks >= 40) clearInterval(timer);
}, 250);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "ble-radar-card")) {
  window.customCards.push({
    type: "ble-radar-card",
    name: "BLE Radar",
    description: "Floor plan with the live position of the tracked devices.",
    preview: false,
  });
}
