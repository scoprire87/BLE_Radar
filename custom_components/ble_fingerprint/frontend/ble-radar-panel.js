// "BLE Radar" page in the Home Assistant sidebar.
import { pointLabel, translator } from "./ble-radar-i18n.js";
import {
  BASE_STYLE, centroid, drawMap, escapeHtml, floorProxies, floorSize, floorZones,
  forgetImage, loadConfig, loadImage, markerSize, svgEl, trackerStates,
} from "./ble-radar-common.js";

const TABS = ["map", "plan", "calibrate", "devices"];
const MAX_IMAGE_SIDE = 2400;

class BleRadarPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "map";
    this._floorId = null;
    this._config = null;
    this._image = null;
    this._mode = null; // draw | scale | shape | proxy | null
    this._draft = [];
    this._editZone = null;
    this._message = null;
    this._devices = null;
    this._calib = { tracker: null, room: null, points: [], index: 0, duration: 15, busy: false };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    this._t = translator(hass);
    if (first) this._load();
    else this._onHassUpdate();
  }

  set narrow(_value) {}
  set panel(_value) {}

  // ------------------------------------------------------------ data

  async _load({ floorId } = {}) {
    try {
      this._config = await loadConfig(this._hass);
    } catch (err) {
      this._config = null;
      this._message = { error: true, text: this._t("error.config", { error: err.message || err }) };
      this._render();
      return;
    }
    const floors = Object.keys(this._config.floors || {});
    if (floorId !== undefined) this._floorId = floorId;
    if (!this._floorId || !floors.includes(this._floorId)) this._floorId = floors[0] || null;
    this._image = this._floorId ? await loadImage(this._hass, this._floorId).catch(() => null) : null;
    this._render();
  }

  async _ws(type, data = {}, okText = null) {
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: `ble_fingerprint/${type}`, ...data });
      if (okText) this._message = { text: okText };
      return result;
    } catch (err) {
      this._message = { error: true, text: err.message || String(err) };
      this._render();
      throw err;
    }
  }

  async _service(service, data, returnResponse = false) {
    const message = { type: "call_service", domain: "ble_fingerprint", service, service_data: data };
    if (returnResponse) message.return_response = true;
    const result = await this._hass.connection.sendMessagePromise(message);
    return result?.response;
  }

  get _floor() {
    return this._floorId ? this._config.floors[this._floorId] : null;
  }

  get _size() {
    return floorSize(this._floor);
  }

  _onHassUpdate() {
    if (!this._config || this._mode === "shape" || this._mode === "proxy") return;
    if (this._tab === "map" || this._tab === "calibrate") {
      this._drawOverlay();
      if (this._tab === "map") this._renderTrackerList();
    }
  }

  // ------------------------------------------------------------ rendering

  _render() {
    const root = this.shadowRoot;
    const t = this._t;
    root.innerHTML = `
      <style>${BASE_STYLE}${PANEL_STYLE}</style>
      <div class="toolbar-top">
        <div class="title">BLE Radar</div>
        <nav>${TABS.map((id) =>
          `<button class="tab ${id === this._tab ? "active" : ""}" data-tab="${id}">${t(`tab.${id}`)}</button>`).join("")}
        </nav>
      </div>
      <div class="content">
        ${this._message ? `<div class="message ${this._message.error ? "error" : ""}">${escapeHtml(this._message.text)}
          <button class="link" data-action="dismiss">${t("close")}</button></div>` : ""}
        <div id="body"></div>
      </div>`;
    root.querySelectorAll(".tab").forEach((btn) =>
      btn.addEventListener("click", () => this._switchTab(btn.dataset.tab)));
    root.querySelector("[data-action=dismiss]")?.addEventListener("click", () => {
      this._message = null; this._render();
    });

    const body = root.getElementById("body");
    if (!this._config) {
      body.innerHTML = `<div class="empty">${t("loading")}</div>`;
      return;
    }
    if (this._tab === "map") this._renderMapTab(body);
    else if (this._tab === "plan") this._renderPlanTab(body);
    else if (this._tab === "calibrate") this._renderCalibrateTab(body);
    else this._renderDevicesTab(body);
  }

  _switchTab(tab) {
    this._tab = tab;
    this._mode = null;
    this._draft = [];
    this._editZone = null;
    this._message = null;
    if (tab === "devices" && !this._devices) this._loadDevices();
    this._render();
  }

  _floorSelect() {
    const floors = Object.entries(this._config.floors || {});
    if (!floors.length) return "";
    return `<select id="floor">${floors.map(([id, f]) =>
      `<option value="${id}" ${id === this._floorId ? "selected" : ""}>${escapeHtml(f.name)}</option>`).join("")}</select>`;
  }

  _bindFloorSelect(container) {
    container.querySelector("#floor")?.addEventListener("change", (ev) => {
      this._mode = null; this._draft = []; this._editZone = null;
      this._load({ floorId: ev.target.value });
    });
  }

  _mapHtml() {
    if (!this._floorId) return "";
    const { width, height } = this._size;
    const base = this._image
      ? `<img src="${this._image}" alt="" draggable="false">`
      : `<svg class="blank" viewBox="0 0 ${width} ${height}"><rect width="${width}" height="${height}" fill="var(--card-background-color, #fff)"/></svg>`;
    return `<div class="map">${base}<svg id="overlay" viewBox="0 0 ${width} ${height}"></svg></div>`;
  }

  _toMap(ev) {
    const svg = this.shadowRoot.getElementById("overlay");
    const rect = svg.getBoundingClientRect();
    const { width, height } = this._size;
    return [
      Math.round(((ev.clientX - rect.left) * width) / rect.width * 10) / 10,
      Math.round(((ev.clientY - rect.top) * height) / rect.height * 10) / 10,
    ];
  }

  _drawOverlay() {
    const svg = this.shadowRoot.getElementById("overlay");
    if (!svg) return;
    svg.innerHTML = "";
    const size = this._size;
    const layers = {
      map: { vertices: false, proxyLabels: false },
      plan: { vertices: true, proxyLabels: true, trackers: false },
      calibrate: { vertices: true, proxyLabels: false },
    }[this._tab] || {};
    drawMap(svg, { hass: this._hass, config: this._config, floorId: this._floorId, size, layers });
    const shown = svg.getBoundingClientRect().width;
    const r = shown > 0 ? (9 * size.width) / shown : markerSize(size);

    if (this._tab === "plan") this._drawPlanOverlay(svg, r);
    if (this._tab === "calibrate") this._drawCalibrationOverlay(svg, r);
  }

  // ------------------------------------------------------------ Map tab

  _renderMapTab(body) {
    const t = this._t;
    if (!this._floorId) {
      body.innerHTML = `<div class="empty">${t("map.no_floor")}</div>`;
      return;
    }
    body.innerHTML = `
      <div class="row">${this._floorSelect()}</div>
      ${this._mapHtml()}
      <div id="trackers"></div>
      <p class="hint">${t("map.legend")}</p>`;
    this._bindFloorSelect(body);
    this._drawOverlay();
    this._renderTrackerList();
  }

  _renderTrackerList() {
    const box = this.shadowRoot.getElementById("trackers");
    if (!box) return;
    const t = this._t;
    const trackers = trackerStates(this._hass, this._config);
    if (!trackers.length) {
      box.innerHTML = `<div class="empty">${t("map.no_trackers")}</div>`;
      return;
    }
    box.innerHTML = `<table><tr><th></th><th>${t("col.device")}</th><th>${t("col.room")}</th><th>${t("col.floor")}</th><th>${t("col.confidence")}</th><th>${t("col.method")}</th></tr>
      ${trackers.map((tr) => {
        const a = tr.state?.attributes || {};
        return `<tr><td><span class="dot" style="background:${tr.color}"></span></td>
          <td>${escapeHtml(tr.name)}</td><td>${escapeHtml(roomState(this._hass, tr.state))}</td>
          <td>${escapeHtml(a.floor_name ?? "—")}</td>
          <td>${a.confidence != null ? Math.round(a.confidence * 100) + "%" : "—"}</td>
          <td>${a.method ? escapeHtml(t(`method.${a.method}`)) : "—"}</td></tr>`;
      }).join("")}</table>`;
  }

  // ------------------------------------------------------------ Floor plan tab

  _renderPlanTab(body) {
    const t = this._t;
    const floor = this._floor;
    const zones = this._floorId ? floorZones(this._config, this._floorId) : [];
    const rooms = this._config.rooms || {};
    const scale = floor?.scale
      ? t("plan.scale_value", { value: Number(floor.scale).toFixed(1) }) : t("plan.scale_unset");
    body.innerHTML = `
      <div class="card">
        <div class="row">
          ${this._floorSelect()}
          <button data-action="new-floor">${t("plan.new_floor")}</button>
        </div>
        ${floor ? `
        <div class="row">
          <label>${t("plan.name")} <input id="floor-name" value="${escapeHtml(floor.name)}"></label>
          <button data-action="rename-floor">${t("plan.rename")}</button>
          <label class="file">${t("plan.upload")}<input id="upload" type="file" accept="image/png,image/jpeg,image/webp"></label>
          <button data-action="scale" class="${this._mode === "scale" ? "active" : ""}">${t("plan.set_scale")}</button>
          <span class="muted">${t("plan.scale", { value: scale })}</span>
          <button data-action="delete-floor" class="danger">${t("plan.delete_floor")}</button>
        </div>` : `<div class="empty">${t("plan.no_floor")}</div>`}
      </div>
      ${floor ? `
      <div class="row tools">
        <button data-action="draw" class="${this._mode === "draw" ? "active" : ""}">${t("plan.draw")}</button>
        <button data-action="proxy" class="${this._mode === "proxy" ? "active" : ""}">${t("plan.move_proxy")}</button>
        <span id="mode-help" class="muted">${this._modeHelp()}</span>
      </div>
      <div id="draft-actions" class="row"></div>
      ${this._mapHtml()}
      <h3>${t("plan.rooms_title")}</h3>
      ${zones.length ? `<table><tr><th></th><th>${t("col.room")}</th><th>${t("col.points")}</th><th></th></tr>
        ${zones.map((z) => `<tr>
          <td><span class="dot" style="background:${z.color}"></span></td>
          <td>${escapeHtml(z.name)}</td>
          <td>${Object.keys(rooms[z.name] || {}).length}</td>
          <td class="actions">
            <button data-zone-action="rename" data-zone="${escapeHtml(z.name)}">${t("plan.rename")}</button>
            <button data-zone-action="shape" data-zone="${escapeHtml(z.name)}">${t("plan.edit_shape")}</button>
            <button data-zone-action="delete" data-zone="${escapeHtml(z.name)}" class="danger">${t("plan.delete")}</button>
          </td></tr>`).join("")}</table>`
        : `<div class="empty">${t("plan.no_rooms")}</div>`}
      ` : ""}`;

    this._bindFloorSelect(body);
    body.querySelector("[data-action=new-floor]").addEventListener("click", () => this._newFloor());
    if (!floor) return;
    body.querySelector("[data-action=rename-floor]").addEventListener("click", () => this._renameFloor());
    body.querySelector("[data-action=delete-floor]").addEventListener("click", () => this._deleteFloor());
    body.querySelector("#upload").addEventListener("change", (ev) => this._uploadImage(ev.target.files[0]));
    body.querySelector("[data-action=scale]").addEventListener("click", () => this._setMode("scale"));
    body.querySelector("[data-action=draw]").addEventListener("click", () => this._setMode("draw"));
    body.querySelector("[data-action=proxy]").addEventListener("click", () => this._setMode("proxy"));
    body.querySelectorAll("[data-zone-action]").forEach((btn) => btn.addEventListener("click", () =>
      this._zoneAction(btn.dataset.zoneAction, btn.dataset.zone)));

    const svg = this.shadowRoot.getElementById("overlay");
    svg.addEventListener("click", (ev) => this._planClick(ev));
    svg.addEventListener("pointerdown", (ev) => this._planPointerDown(ev));
    svg.addEventListener("dblclick", (ev) => {
      // double click on a corner removes it (at least 3 corners)
      const handle = ev.target.dataset?.handle;
      if (this._mode !== "shape" || handle === undefined || this._draft.length <= 3) return;
      this._draft.splice(Number(handle), 1);
      this._drawOverlay();
    });
    this._drawOverlay();
    this._renderDraftActions();
  }

  _modeHelp() {
    const t = this._t;
    switch (this._mode) {
      case "draw": return t("help.draw");
      case "scale": return t("help.scale");
      case "shape": return t("help.shape", { room: escapeHtml(this._editZone) });
      case "proxy": return t("help.proxy");
      default: return "";
    }
  }

  _setMode(mode) {
    this._mode = this._mode === mode ? null : mode;
    this._draft = [];
    this._editZone = null;
    this._render();
  }

  _renderDraftActions() {
    const t = this._t;
    const box = this.shadowRoot.getElementById("draft-actions");
    if (!box) return;
    box.innerHTML = "";
    if (this._mode === "draw" && this._draft.length) {
      box.innerHTML = `<label>${t("draft.room_name")} <input id="zone-name" placeholder="${t("draft.room_placeholder")}"></label>
        <button data-d="save" ${this._draft.length < 3 ? "disabled" : ""}>${t("draft.save_room")}</button>
        <button data-d="undo">${t("draft.undo")}</button>
        <button data-d="cancel">${t("draft.cancel")}</button>`;
    } else if (this._mode === "scale" && this._draft.length === 2) {
      box.innerHTML = `<label>${t("draft.distance")} <input id="meters" type="number" min="0.1" step="0.01" placeholder="${t("draft.metres")}"> m</label>
        <button data-d="save-scale">${t("draft.save_scale")}</button><button data-d="cancel">${t("draft.cancel")}</button>`;
    } else if (this._mode === "shape") {
      box.innerHTML = `<button data-d="save-shape">${t("draft.save_shape")}</button><button data-d="cancel">${t("draft.cancel")}</button>`;
    }
    box.querySelector("[data-d=save]")?.addEventListener("click", () => this._saveDraftZone());
    box.querySelector("[data-d=undo]")?.addEventListener("click", () => {
      this._draft.pop(); this._drawOverlay(); this._renderDraftActions();
    });
    box.querySelector("[data-d=cancel]")?.addEventListener("click", () => this._setMode(null));
    box.querySelector("[data-d=save-scale]")?.addEventListener("click", () => this._saveScale());
    box.querySelector("[data-d=save-shape]")?.addEventListener("click", () => this._saveShape());
  }

  _drawPlanOverlay(svg, r) {
    if (this._mode === "draw" && this._draft.length) {
      svgEl("polyline", {
        points: this._draft.map((p) => p.join(",")).join(" "),
        fill: "rgba(3,169,244,0.15)", stroke: "#03a9f4", "stroke-width": r / 3,
        "stroke-dasharray": `${r} ${r / 2}`,
      }, svg);
      this._draft.forEach(([x, y]) => svgEl("circle", { cx: x, cy: y, r: r * 0.6, fill: "#03a9f4" }, svg));
    }
    if (this._mode === "scale") {
      this._draft.forEach(([x, y]) => svgEl("circle", { cx: x, cy: y, r: r * 0.6, fill: "#ff5722" }, svg));
      if (this._draft.length === 2) {
        const [[x1, y1], [x2, y2]] = this._draft;
        svgEl("line", { x1, y1, x2, y2, stroke: "#ff5722", "stroke-width": r / 3 }, svg);
      }
    }
    if (this._mode === "shape" && this._draft.length) {
      svgEl("polygon", {
        points: this._draft.map((p) => p.join(",")).join(" "),
        fill: "rgba(255,152,0,0.2)", stroke: "#ff9800", "stroke-width": r / 3, "data-shape": "1",
      }, svg);
      this._draft.forEach(([x, y], i) => svgEl("circle", {
        cx: x, cy: y, r: r * 0.8, fill: "#ff9800", stroke: "#fff", "stroke-width": r / 5,
        "data-handle": i, style: "cursor: grab",
      }, svg));
    }
    if (this._mode === "proxy") {
      svg.querySelectorAll("[data-proxy]").forEach((g) => { g.style.cursor = "grab"; });
    }
  }

  _planClick(ev) {
    if (this._dragged) { this._dragged = false; return; }
    const point = this._toMap(ev);
    if (this._mode === "draw") {
      this._draft.push(point);
      this._drawOverlay();
      const name = this.shadowRoot.getElementById("zone-name")?.value || "";
      this._renderDraftActions();
      const input = this.shadowRoot.getElementById("zone-name");
      if (input) input.value = name;
    } else if (this._mode === "scale") {
      if (this._draft.length >= 2) this._draft = [];
      this._draft.push(point);
      this._drawOverlay();
      this._renderDraftActions();
    } else if (this._mode === "shape") {
      if (ev.target.dataset?.handle !== undefined) return;
      // click near an edge: add a corner there
      const { index, dist } = nearestEdge(this._draft, point);
      const svg = this.shadowRoot.getElementById("overlay");
      const shown = svg.getBoundingClientRect().width || this._size.width;
      if (dist > (18 * this._size.width) / shown) return;
      this._draft.splice(index + 1, 0, point);
      this._drawOverlay();
    } else if (this._mode === "proxy") {
      const g = ev.target.closest?.("[data-proxy]");
      const proxy = g && floorProxies(this._config, this._floorId).find((p) => p.name === g.dataset.proxy);
      if (proxy?.manual && window.confirm(this._t("confirm.reset_proxy", { proxy: proxy.name }))) {
        this._ws("set_proxy", { proxy: proxy.name, reset: true }, this._t("msg.proxy_reset", { proxy: proxy.name }))
          .then(() => this._load());
      }
    }
  }

  _planPointerDown(ev) {
    const svg = this.shadowRoot.getElementById("overlay");
    let move, target;
    if (this._mode === "shape" && ev.target.dataset?.handle !== undefined) {
      const index = Number(ev.target.dataset.handle);
      move = (point) => { this._draft[index] = point; this._drawOverlay(); };
    } else if (this._mode === "proxy" && (target = ev.target.closest?.("[data-proxy]"))) {
      const name = target.dataset.proxy;
      let last = null;
      move = (point) => {
        last = point;
        target.setAttribute("transform", `translate(${point[0]},${point[1]})`);
      };
      const up = () => {
        svg.removeEventListener("pointerup", up);
        if (last) {
          this._ws("set_proxy", { proxy: name, floor_id: this._floorId, x: last[0], y: last[1] },
            this._t("msg.proxy_saved", { proxy: name })).then(() => this._load());
        }
      };
      svg.addEventListener("pointerup", up);
    } else {
      return;
    }
    ev.preventDefault();
    svg.setPointerCapture(ev.pointerId);
    let moved = false;
    const onMove = (e) => { moved = true; move(this._toMap(e)); };
    const onUp = () => {
      svg.removeEventListener("pointermove", onMove);
      svg.removeEventListener("pointerup", onUp);
      this._dragged = moved;
    };
    svg.addEventListener("pointermove", onMove);
    svg.addEventListener("pointerup", onUp);
  }

  async _newFloor() {
    const name = window.prompt(this._t("prompt.floor_name"));
    if (!name?.trim()) return;
    const result = await this._ws("save_floor", { name: name.trim() }, this._t("msg.floor_created", { floor: name.trim() }));
    this._mode = null;
    await this._load({ floorId: result.floor_id });
  }

  async _renameFloor() {
    const name = this.shadowRoot.getElementById("floor-name").value.trim();
    if (!name) return;
    await this._ws("save_floor", { floor_id: this._floorId, name }, this._t("msg.floor_renamed"));
    await this._load();
  }

  async _deleteFloor() {
    const floor = this._floor;
    if (!window.confirm(this._t("confirm.delete_floor", { floor: floor.name }))) return;
    await this._ws("delete_floor", { floor_id: this._floorId }, this._t("msg.floor_deleted"));
    forgetImage(this._floorId);
    this._floorId = null;
    await this._load();
  }

  async _uploadImage(file) {
    if (!file) return;
    try {
      const { dataUrl, width, height } = await prepareImage(file, this._t);
      await this._ws("save_floor", {
        floor_id: this._floorId, name: this._floor.name, image: dataUrl, width, height,
      }, this._t("msg.image_uploaded"));
      forgetImage(this._floorId);
      await this._load();
    } catch (err) {
      this._message = { error: true, text: this._t("msg.image_invalid", { error: err.message || err }) };
      this._render();
    }
  }

  async _saveScale() {
    const meters = Number(this.shadowRoot.getElementById("meters").value);
    if (!(meters > 0)) return;
    const [[x1, y1], [x2, y2]] = this._draft;
    const pixels = Math.hypot(x2 - x1, y2 - y1);
    await this._ws("save_floor", { floor_id: this._floorId, name: this._floor.name, scale: pixels / meters },
      this._t("msg.scale_saved", { value: (pixels / meters).toFixed(1) }));
    this._mode = null; this._draft = [];
    await this._load();
  }

  async _saveDraftZone() {
    const name = this.shadowRoot.getElementById("zone-name").value.trim();
    if (!name) { window.alert(this._t("alert.room_name")); return; }
    await this._ws("save_zone", { name, floor_id: this._floorId, polygon: this._draft },
      this._t("msg.room_saved", { room: name }));
    this._mode = null; this._draft = [];
    await this._load();
  }

  async _saveShape() {
    await this._ws("save_zone", {
      name: this._editZone, old_name: this._editZone, floor_id: this._floorId, polygon: this._draft,
    }, this._t("msg.shape_saved", { room: this._editZone }));
    this._mode = null; this._draft = []; this._editZone = null;
    await this._load();
  }

  async _zoneAction(action, name) {
    const zone = this._config.zones[name];
    if (action === "rename") {
      const newName = window.prompt(this._t("prompt.room_name"), name)?.trim();
      if (!newName || newName === name) return;
      await this._ws("save_zone", { name: newName, old_name: name, floor_id: zone.floor, polygon: zone.polygon },
        this._t("msg.room_renamed"));
      await this._load();
    } else if (action === "shape") {
      this._mode = "shape";
      this._editZone = name;
      this._draft = zone.polygon.map((p) => [...p]);
      this._render();
    } else if (action === "delete") {
      if (!window.confirm(this._t("confirm.delete_room", { room: name }))) return;
      await this._ws("delete_zone", { name }, this._t("msg.room_deleted"));
      await this._load();
    }
  }

  // ------------------------------------------------------------ Calibration tab

  _renderCalibrateTab(body) {
    const t = this._t;
    const trackers = trackerStates(this._hass, this._config).filter((t) => t.entity_id);
    const zones = this._floorId ? floorZones(this._config, this._floorId) : [];
    const c = this._calib;
    if (!this._floorId || !zones.length || !trackers.length) {
      body.innerHTML = `<div class="empty">${t("calib.requirements")}<br>
        ${!this._floorId || !zones.length ? `${t("calib.need_room")}<br>` : ""}
        ${!trackers.length ? t("calib.need_tracker") : ""}</div>`;
      return;
    }
    if (!trackers.some((tr) => tr.id === c.tracker)) c.tracker = trackers[0].id;
    if (!zones.some((z) => z.name === c.room)) { c.room = null; c.points = []; }

    const point = c.points[c.index];
    const done = c.points.filter((p) => p.recorded).length;
    body.innerHTML = `
      <div class="card">
        <div class="row">
          ${this._floorSelect()}
          <label>${t("calib.device")} <select id="c-tracker">${trackers.map((tr) =>
            `<option value="${tr.id}" ${tr.id === c.tracker ? "selected" : ""}>${escapeHtml(tr.name)}</option>`).join("")}</select></label>
          <label>${t("calib.room")} <select id="c-room"><option value="">${t("calib.choose")}</option>${zones.map((z) =>
            `<option value="${escapeHtml(z.name)}" ${z.name === c.room ? "selected" : ""}>${escapeHtml(z.name)}</option>`).join("")}</select></label>
          <label>${t("calib.duration")} <select id="c-duration">${[5, 10, 15, 20, 30].map((d) =>
            `<option value="${d}" ${d === c.duration ? "selected" : ""}>${d} s</option>`).join("")}</select></label>
        </div>
        ${c.room ? `
        <div class="guide">
          ${point ? `<div class="step">${t("calib.step", { n: c.index + 1, total: c.points.length, label: escapeHtml(pointLabel(t, point)) })}
            ${point.recorded ? `<span class="ok">${t("calib.already")}</span>` : ""}</div>
            <div>${t("calib.instructions")}</div>` : ""}
          <div class="row">
            <button data-c="record" class="primary" ${c.busy || !point ? "disabled" : ""}>${c.busy ? t("calib.recording", { seconds: c.remaining ?? "" }) : t("calib.record")}</button>
            <button data-c="prev" ${c.busy || c.index === 0 ? "disabled" : ""}>${t("calib.back")}</button>
            <button data-c="next" ${c.busy || c.index >= c.points.length - 1 ? "disabled" : ""}>${t("calib.skip")}</button>
            ${point?.recorded ? `<button data-c="delete" class="danger" ${c.busy ? "disabled" : ""}>${t("calib.delete_point")}</button>` : ""}
            <span class="muted">${t("calib.progress", { done, total: c.points.length })}</span>
          </div>
        </div>` : `<div class="muted">${t("calib.pick_room")}</div>`}
      </div>
      ${this._mapHtml()}
      <p class="hint">${t("calib.legend")}</p>`;

    this._bindFloorSelect(body);
    body.querySelector("#c-tracker").addEventListener("change", (ev) => { c.tracker = ev.target.value; this._render(); });
    body.querySelector("#c-room").addEventListener("change", (ev) => this._loadGuidedPoints(ev.target.value));
    body.querySelector("#c-duration").addEventListener("change", (ev) => { c.duration = Number(ev.target.value); });
    body.querySelector("[data-c=record]")?.addEventListener("click", () => this._recordPoint());
    body.querySelector("[data-c=prev]")?.addEventListener("click", () => { c.index -= 1; this._render(); });
    body.querySelector("[data-c=next]")?.addEventListener("click", () => { c.index += 1; this._render(); });
    body.querySelector("[data-c=delete]")?.addEventListener("click", () => this._deletePoint());
    this.shadowRoot.getElementById("overlay").addEventListener("click", (ev) => this._calibrationClick(ev));
    this._drawOverlay();
  }

  async _loadGuidedPoints(room) {
    const c = this._calib;
    c.room = room || null;
    c.points = [];
    c.index = 0;
    if (room) {
      const result = await this._ws("guided_points", { room });
      c.points = result.points;
      // free points already recorded in this room
      for (const [id, v] of Object.entries((this._config.rooms || {})[room] || {})) {
        if (!c.points.some((p) => p.vertex_id === id) && v.coords?.x != null) {
          c.points.push({ vertex_id: id, kind: "free", x: v.coords.x, y: v.coords.y, recorded: true });
        }
      }
      const next = c.points.findIndex((p) => !p.recorded);
      c.index = next >= 0 ? next : 0;
    }
    this._render();
  }

  _drawCalibrationOverlay(svg, r) {
    const c = this._calib;
    c.points.forEach((p, i) => {
      const current = i === c.index;
      const g = svgEl("g", { transform: `translate(${p.x},${p.y})` }, svg);
      if (current) {
        const pulse = svgEl("circle", { r: r * 2.2, fill: "none", stroke: "#ff9800", "stroke-width": r / 3 }, g);
        svgEl("animate", { attributeName: "r", values: `${r * 1.4};${r * 2.6};${r * 1.4}`, dur: "1.6s", repeatCount: "indefinite" }, pulse);
      }
      svgEl("circle", {
        r: current ? r * 1.1 : r * 0.8,
        fill: p.recorded ? "#2e7d32" : current ? "#ff9800" : "var(--card-background-color, #fff)",
        stroke: p.recorded ? "#fff" : "#ff9800", "stroke-width": r / 4,
      }, g);
      const label = svgEl("text", {
        "text-anchor": "middle", "dominant-baseline": "central", "font-size": r, "font-weight": "700",
        fill: p.recorded || current ? "#fff" : "#ff9800", "pointer-events": "none",
      }, g);
      label.textContent = String(i + 1);
    });
  }

  _calibrationClick(ev) {
    const c = this._calib;
    if (!c.room || c.busy) return;
    const [x, y] = this._toMap(ev);
    let n = 1;
    while (c.points.some((p) => p.vertex_id === `free${n}`)) n += 1;
    c.points.push({ vertex_id: `free${n}`, kind: "free", x, y, recorded: false });
    c.index = c.points.length - 1;
    this._render();
  }

  async _recordPoint() {
    const c = this._calib;
    const point = c.points[c.index];
    const tracker = (this._config.trackers || []).find((t) => t.id === c.tracker);
    if (!point || !tracker?.entity_id) return;
    c.busy = true;
    c.remaining = c.duration;
    this._render();
    const timer = setInterval(() => {
      c.remaining = Math.max(0, c.remaining - 1);
      const btn = this.shadowRoot.querySelector("[data-c=record]");
      if (btn) btn.textContent = this._t("calib.recording", { seconds: c.remaining });
    }, 1000);
    try {
      const response = await this._service("record_vertex", {
        entity_id: tracker.entity_id,
        room: c.room,
        vertex_id: point.vertex_id,
        x: point.x,
        y: point.y,
        floor: this._floorId,
        duration: c.duration,
      }, true);
      point.recorded = true;
      const proxies = Object.keys(response?.distances || {}).length;
      this._message = { text: this._t("msg.point_recorded", { point: pointLabel(this._t, point), count: proxies }) };
      const next = c.points.findIndex((p, i) => i > c.index && !p.recorded);
      if (next >= 0) c.index = next;
      else this._message.text += c.points.every((p) => p.recorded) ? this._t("msg.room_done") : "";
    } catch (err) {
      this._message = { error: true, text: this._t("msg.record_failed", { error: err.message || err }) };
    } finally {
      clearInterval(timer);
      c.busy = false;
      this._config = await loadConfig(this._hass).catch(() => this._config);
      this._render();
    }
  }

  async _deletePoint() {
    const c = this._calib;
    const point = c.points[c.index];
    if (!point || !window.confirm(this._t("confirm.delete_point", { point: pointLabel(this._t, point) }))) return;
    try {
      await this._service("delete_vertex", { room: c.room, vertex_id: point.vertex_id });
      point.recorded = false;
      if (point.kind === "free") { c.points.splice(c.index, 1); c.index = Math.max(0, c.index - 1); }
      this._message = { text: this._t("msg.point_deleted") };
    } catch (err) {
      this._message = { error: true, text: err.message || String(err) };
    }
    this._config = await loadConfig(this._hass).catch(() => this._config);
    this._render();
  }

  // ------------------------------------------------------------ Devices tab

  async _loadDevices() {
    try {
      const result = await this._ws("devices");
      this._devices = result.devices;
    } catch (_err) {
      this._devices = [];
    }
    if (this._tab === "devices") this._render();
  }

  _renderDevicesTab(body) {
    const t = this._t;
    const trackers = this._config.trackers || [];
    const devices = this._devices;
    const used = new Set(trackers.map((t) => t.device_id).filter(Boolean));
    const available = (devices || []).filter((d) => !used.has(d.device_id));
    const deviceName = (id) => (devices || []).find((d) => d.device_id === id)?.name || id || t("devices.manual");
    body.innerHTML = `
      <div class="card">
        <h3>${t("devices.tracked")}</h3>
        ${trackers.length ? `<table><tr><th>${t("col.name")}</th><th>${t("col.device")}</th><th>${t("col.sensors")}</th><th>${t("col.entity")}</th><th></th></tr>
          ${trackers.map((tr, i) => `<tr>
            <td><input data-name="${i}" value="${escapeHtml(tr.name)}"></td>
            <td>${escapeHtml(deviceName(tr.device_id))}</td>
            <td>${tr.proxy_sensors.length}</td>
            <td class="muted">${escapeHtml(tr.entity_id || "—")}</td>
            <td><button data-remove="${i}" class="danger">${t("devices.remove")}</button></td></tr>`).join("")}</table>
          <div class="row"><button data-action="save-names">${t("devices.save_names")}</button></div>`
        : `<div class="empty">${t("devices.none")}</div>`}
      </div>
      <div class="card">
        <h3>${t("devices.add_title")}</h3>
        ${devices === null ? `<div class="muted">${t("loading")}</div>` : available.length ? `
          <div class="row">
            <select id="new-device">${available.map((d) =>
              `<option value="${d.device_id}">${t("devices.option", { name: escapeHtml(d.name), bermuda: d.bermuda ? " (Bermuda)" : "", count: d.sensors.length })}</option>`).join("")}</select>
            <input id="new-name" placeholder="${t("devices.name_placeholder")}">
            <button data-action="add" class="primary">${t("devices.add")}</button>
          </div>
          <p class="hint">${t("devices.hint")}</p>`
        : `<div class="empty">${t("devices.empty")}</div>`}
      </div>`;

    body.querySelectorAll("[data-remove]").forEach((btn) => btn.addEventListener("click", () => {
      const tracker = trackers[Number(btn.dataset.remove)];
      if (!window.confirm(t("confirm.remove_tracker", { name: tracker.name }))) return;
      this._saveTrackers(trackers.filter((x) => x !== tracker), t("msg.tracker_removed"));
    }));
    body.querySelector("[data-action=save-names]")?.addEventListener("click", () => {
      const updated = trackers.map((tr, i) => ({ ...tr, name: body.querySelector(`[data-name="${i}"]`).value.trim() || tr.name }));
      this._saveTrackers(updated, t("msg.names_saved"));
    });
    body.querySelector("[data-action=add]")?.addEventListener("click", () => {
      const deviceId = body.querySelector("#new-device").value;
      const device = available.find((d) => d.device_id === deviceId);
      const name = body.querySelector("#new-name").value.trim() || t("devices.default_name", { device: device.name });
      this._saveTrackers([...trackers, { name, device_id: deviceId, sensors: [] }], t("msg.tracker_added", { name }));
    });
  }

  async _saveTrackers(trackers, okText) {
    await this._ws("save_trackers", {
      trackers: trackers.map((tr) => ({ id: tr.id, name: tr.name, device_id: tr.device_id || null, sensors: tr.sensors || [] })),
    }, okText);
    // the integration reloads: wait for the sensors to be ready
    setTimeout(() => this._load(), 1500);
    this._render();
  }
}

function nearestEdge(polygon, [px, py]) {
  let best = 0, bestDist = Infinity;
  for (let i = 0; i < polygon.length; i++) {
    const [x1, y1] = polygon[i];
    const [x2, y2] = polygon[(i + 1) % polygon.length];
    const dx = x2 - x1, dy = y2 - y1;
    const len2 = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / len2));
    const dist = Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
    if (dist < bestDist) { best = i; bestDist = dist; }
  }
  return { index: best, dist: bestDist };
}

// Room shown in the table: the sensor state, or Home Assistant's "unknown"
function roomState(hass, state) {
  if (!state || state.state === "unknown" || state.state === "unavailable") {
    return hass.localize?.(`state.default.${state?.state || "unknown"}`) || "—";
  }
  return state.state;
}

// Downscale large floor plans before sending them to Home Assistant
function prepareImage(file, t) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(t("image.read_error")));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error(t("image.format_error")));
      img.onload = () => {
        const ratio = Math.min(1, MAX_IMAGE_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
        const width = Math.round(img.naturalWidth * ratio);
        const height = Math.round(img.naturalHeight * ratio);
        if (ratio === 1 && file.size < 2 * 1024 * 1024) {
          resolve({ dataUrl: reader.result, width, height });
          return;
        }
        const canvas = document.createElement("canvas");
        canvas.width = width; canvas.height = height;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, width, height);
        ctx.drawImage(img, 0, 0, width, height);
        resolve({ dataUrl: canvas.toDataURL("image/jpeg", 0.88), width, height });
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

const PANEL_STYLE = `
  :host {
    display: block; min-height: 100vh;
    background: var(--primary-background-color, #fafafa);
    color: var(--primary-text-color, #212121);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
  }
  .toolbar-top {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 8px 16px; background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, #fff);
  }
  .title { font-size: 20px; font-weight: 500; margin-right: 8px; }
  nav { display: flex; gap: 4px; flex-wrap: wrap; }
  .tab {
    background: transparent; color: inherit; border: 0; padding: 10px 14px;
    border-bottom: 2px solid transparent; cursor: pointer; font-size: 14px; opacity: 0.8;
  }
  .tab.active { border-bottom-color: currentColor; opacity: 1; }
  .content { padding: 16px; max-width: 1400px; margin: 0 auto; }
  .card {
    background: var(--card-background-color, #fff); border-radius: 12px; padding: 12px 16px;
    margin-bottom: 12px; box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,0.12));
  }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0; }
  label { display: inline-flex; gap: 6px; align-items: center; }
  button, select, input {
    font: inherit; padding: 6px 10px; border-radius: 6px;
    border: 1px solid var(--divider-color, #ccc);
    background: var(--card-background-color, #fff); color: var(--primary-text-color);
  }
  button { cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.active, button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button.danger { color: var(--error-color, #db4437); }
  button.link { border: 0; background: none; text-decoration: underline; color: inherit; padding: 0 4px; }
  label.file { border: 1px solid var(--divider-color, #ccc); border-radius: 6px; padding: 6px 10px; cursor: pointer; }
  label.file input { display: none; }
  .muted, .hint { color: var(--secondary-text-color); font-size: 13px; }
  .message { padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; background: rgba(3,169,244,0.12); }
  .message.error { background: rgba(219,68,55,0.15); }
  .tools { margin-top: 0; }
  /* reserved space: the map does not move while drawing */
  #draft-actions { min-height: 40px; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--divider-color, #eee); }
  td.actions { text-align: right; white-space: nowrap; }
  .dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; }
  .guide { margin-top: 8px; line-height: 1.5; }
  .step { font-size: 16px; }
  .ok { color: #2e7d32; margin-left: 8px; }
  h3 { margin: 16px 0 4px; font-weight: 500; }
  #overlay { touch-action: none; }
`;

customElements.define("ble-radar-panel", BleRadarPanel);
