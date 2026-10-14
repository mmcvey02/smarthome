"use strict";

// ---------------------------------------------------------------- helpers

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(formatError(data.detail) || res.statusText);
    err.detail = data.detail;
    throw err;
  }
  return data;
}

function formatError(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map(e => `${(e.loc || []).filter(x => x !== "body").join(" › ")}: ${e.msg}`).join("\n");
  }
  return JSON.stringify(detail);
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 6000 : 2500);
}

// Approximate colour of a white light at a given Kelvin (Tanner Helland's fit).
function kelvinToRgb(k) {
  const t = k / 100;
  let r, g, b;
  if (t <= 66) { r = 255; g = 99.47 * Math.log(t) - 161.12; }
  else { r = 329.7 * Math.pow(t - 60, -0.1332); g = 288.12 * Math.pow(t - 60, -0.0755); }
  if (t >= 66) b = 255; else if (t <= 19) b = 0; else b = 138.52 * Math.log(t - 10) - 305.04;
  const c = v => Math.round(Math.max(0, Math.min(255, v)));
  return `rgb(${c(r)},${c(g)},${c(b)})`;
}
const rgbToHex = ([r, g, b]) => "#" + [r, g, b].map(x => x.toString(16).padStart(2, "0")).join("");
const hexToRgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
const slug = s => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 64) || "routine";
const clone = o => JSON.parse(JSON.stringify(o));

const ICONS = { light: "💡", plug: "🔌", climate: "❄️", vacuum: "🧹", sensor: "📟", other: "⚙️" };
const KIND_LABELS = { light: "Lights", plug: "Plugs", climate: "Climate", vacuum: "Vacuums", sensor: "Sensors", other: "Other" };
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const MODES = ["auto", "cool", "heat", "dry", "fan"];
const FANS = ["auto", "silent", "low", "medium", "high", "max"];
const VACUUM_CMDS = ["start", "pause", "resume", "stop", "dock"];

// ---------------------------------------------------------------- state

const state = {
  status: null,
  devices: new Map(),
  routines: [],
  roomFilter: localGet("roomFilter") || "All",
};

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

const unit = () => (state.status?.temperature_unit === "C" ? "°C" : "°F");
const devicesSorted = () => [...state.devices.values()].sort((a, b) => a.name.localeCompare(b.name));
const visibleDevices = () => devicesSorted().filter(d => !d.attributes.hidden);
const rooms = () => [...new Set(visibleDevices().map(d => d.room).filter(Boolean))].sort();

// ---------------------------------------------------------------- tabs

document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => showView(btn.dataset.view)));

function showView(view) {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("hidden", v.id !== `view-${view}`));
  localSet("view", view);
  if (view === "routines") loadRoutines();
  if (view === "settings") renderSettings();
}

// ---------------------------------------------------------------- devices view

async function setDeviceState(id, changes) {
  try {
    const d = await api("POST", `/api/devices/${encodeURIComponent(id)}/state`, changes);
    upsertDevice(d);
  } catch (e) {
    toast(e.message, true);
    renderDevices();
  }
}

async function sendCommand(id, command) {
  try {
    upsertDevice(await api("POST", `/api/devices/${encodeURIComponent(id)}/command`, { command }));
  } catch (e) { toast(e.message, true); }
}

function upsertDevice(d) {
  state.devices.set(d.id, d);
  const card = document.querySelector(`.card[data-id="${CSS.escape(d.id)}"]`);
  // Don't yank a slider out from under someone's finger.
  if (card && card.contains(document.activeElement) && document.activeElement.type === "range") return;
  if (card) card.replaceWith(deviceCard(d));
  else renderDevices();
}

function renderDevices() {
  const filter = $("#room-filter");
  const allRooms = rooms();
  const choices = ["All", ...allRooms, ...(visibleDevices().some(d => !d.room) ? ["Unassigned"] : [])];
  if (!choices.includes(state.roomFilter)) state.roomFilter = "All";
  filter.replaceChildren(...choices.map(r => h("button", {
    class: "chip" + (r === state.roomFilter ? " active" : ""),
    onclick: () => { state.roomFilter = r; localSet("roomFilter", r); renderDevices(); },
  }, r)));

  const groups = $("#device-groups");
  const devices = visibleDevices();
  if (!devices.length) {
    groups.replaceChildren(h("div", { class: "empty" },
      "No devices yet. Add your integrations to config.yaml (see README) and restart the hub."));
    return;
  }
  const sections = [];
  for (const room of [...allRooms, null]) {
    const name = room || "Unassigned";
    if (state.roomFilter !== "All" && state.roomFilter !== name) continue;
    const inRoom = devices.filter(d => (d.room || null) === room);
    if (!inRoom.length) continue;
    const lights = inRoom.filter(d => d.capabilities.includes("power") && d.kind === "light");
    sections.push(h("div", { class: "room" },
      h("div", { class: "room-head" },
        h("h2", {}, name),
        room && lights.length ? h("button", { class: "btn small", onclick: () => roomPower(room, true) }, "Lights on") : null,
        room && lights.length ? h("button", { class: "btn small", onclick: () => roomPower(room, false) }, "Off") : null,
      ),
      h("div", { class: "grid" }, inRoom.map(deviceCard)),
    ));
  }
  groups.replaceChildren(...sections);
}

async function roomPower(room, on) {
  try {
    const res = await api("POST", `/api/rooms/${encodeURIComponent(room)}/state`, { on });
    if (res.errors.length) toast(res.errors.join("; "), true);
  } catch (e) { toast(e.message, true); }
}

function powerSwitch(d) {
  return h("label", { class: "switch", title: d.state.on ? "Turn off" : "Turn on" },
    h("input", { type: "checkbox", checked: d.state.on, onchange: e => setDeviceState(d.id, { on: e.target.checked }) }),
    h("span"));
}

function slider(label, { min, max, step = 1, value, cls, fmt, onCommit }) {
  const out = h("output", {}, fmt(value));
  const input = h("input", {
    type: "range", min, max, step, value, class: cls,
    oninput: e => { out.textContent = fmt(+e.target.value); },
    onchange: e => { onCommit(+e.target.value); e.target.blur(); },
  });
  return h("div", { class: "row" }, h("label", {}, label), input, out);
}

function deviceCard(d) {
  const s = d.state;
  const on = d.kind === "vacuum" ? s.status === "cleaning" : !!s.on;
  const sub = [];
  if (!d.available) sub.push("Offline");
  else if (d.kind === "light" || d.kind === "plug") sub.push(on ? (s.brightness != null ? `${s.brightness}%` : "On") : "Off");
  else if (d.kind === "climate") sub.push(on ? `${s.mode} · ${s.target_temp}${unit()}` : "Off");
  else if (d.kind === "vacuum") sub.push(s.status + (s.battery != null ? ` · ${s.battery}%` : ""));
  sub.push(d.attributes.model || d.integration);

  const icon = h("div", { class: "card-icon" }, ICONS[d.kind] || ICONS.other);
  if (d.kind === "light" && on && s.color_temp) icon.style.background = kelvinToRgb(s.color_temp);

  const card = h("div", { class: `card${on ? " is-on" : ""}${d.available ? "" : " unavailable"}`, "data-id": d.id },
    h("div", { class: "card-head" },
      icon,
      h("div", {}, h("div", { class: "card-title" }, d.name), h("div", { class: "card-sub" }, sub.join(" · "))),
      d.capabilities.includes("power") ? powerSwitch(d) : null,
    ));

  if (d.capabilities.includes("brightness")) {
    card.append(slider("Brightness", { min: 1, max: 100, value: s.brightness ?? 100, fmt: v => `${v}%`,
      onCommit: v => setDeviceState(d.id, { brightness: v, on: true }) }));
  }
  if (d.capabilities.includes("color_temp")) {
    card.append(slider("Warmth", { min: 2000, max: 6500, step: 50, value: s.color_temp ?? 3000, cls: "ct",
      fmt: v => `${v}K`, onCommit: v => setDeviceState(d.id, { color_temp: v, on: true }) }));
  }
  if (d.capabilities.includes("color")) {
    card.append(h("div", { class: "row" }, h("label", {}, "Colour"),
      h("input", { type: "color", value: rgbToHex(s.rgb || [255, 255, 255]),
        onchange: e => setDeviceState(d.id, { rgb: hexToRgb(e.target.value), on: true }) })));
  }
  if (d.capabilities.includes("climate")) card.append(...climateControls(d));
  if (d.capabilities.includes("vacuum")) card.append(...vacuumControls(d));
  return card;
}

function climateControls(d) {
  const s = d.state;
  const step = unit() === "°F" ? 1 : 0.5;
  const min = d.attributes.min_temp ?? (unit() === "°F" ? 60 : 16);
  const max = d.attributes.max_temp ?? (unit() === "°F" ? 86 : 30);
  let target = s.target_temp;
  let timer;
  const val = h("span", { class: "val" }, `${target}°`);
  const bump = delta => {
    target = Math.max(min, Math.min(max, +(target + delta).toFixed(1)));
    val.textContent = `${target}°`;
    clearTimeout(timer); // wait for the tapping to stop before talking to the unit
    timer = setTimeout(() => setDeviceState(d.id, { target_temp: target }), 700);
  };
  const modes = d.attributes.modes || MODES;
  const fans = d.attributes.fan_speeds || FANS;
  return [
    h("div", { class: "climate-temps" },
      h("div", {}, h("div", { class: "card-sub" }, "Inside"),
        h("div", { class: "big-temp" }, s.current_temp != null ? `${s.current_temp}${unit()}` : "–")),
      h("div", { class: "stepper" },
        h("button", { class: "btn round", onclick: () => bump(-step), "aria-label": "Cooler" }, "−"),
        val,
        h("button", { class: "btn round", onclick: () => bump(step), "aria-label": "Warmer" }, "+"))),
    h("div", { class: "seg" }, modes.map(m => h("button", {
      class: s.mode === m ? "active" : "", onclick: () => setDeviceState(d.id, { mode: m, on: true }) }, m))),
    h("div", { class: "row" }, h("label", {}, "Fan"),
      h("select", { onchange: e => setDeviceState(d.id, { fan_speed: e.target.value }) },
        fans.map(f => h("option", { value: f, selected: s.fan_speed === f }, f)))),
  ];
}

function vacuumControls(d) {
  const s = d.state;
  const cmds = d.attributes.commands || VACUUM_CMDS;
  const primary = s.status === "cleaning" ? "pause" : s.status === "paused" ? "resume" : "start";
  return [
    h("div", { class: "row" }, h("label", {}, "Battery"),
      h("div", { class: "battery" }, h("div", { style: `width:${s.battery ?? 0}%` })),
      h("output", {}, s.battery != null ? `${s.battery}%` : "–")),
    s.bin_full ? h("div", { class: "card-sub" }, "⚠ Bin full") : null,
    d.attributes.error ? h("div", { class: "card-sub" }, `⚠ ${d.attributes.error}`) : null,
    h("div", { class: "btns" },
      cmds.filter(c => ["start", "pause", "resume"].includes(c) ? c === primary : true)
        .map(c => h("button", { class: "btn small" + (c === primary ? " primary" : ""), onclick: () => sendCommand(d.id, c) },
          c[0].toUpperCase() + c.slice(1)))),
  ].filter(Boolean);
}

// ---------------------------------------------------------------- routines view

async function loadRoutines() {
  try {
    state.routines = await api("GET", "/api/routines");
    renderRoutines();
  } catch (e) { toast(e.message, true); }
}

function describeTarget(t) {
  if (!t) return "";
  if (t.all) return "everything";
  const parts = [];
  if (t.kinds?.length) parts.push(t.kinds.map(k => (KIND_LABELS[k] || k).toLowerCase()).join(", "));
  if (t.rooms?.length) parts.push(t.rooms.join(", "));
  if (t.devices?.length) parts.push(t.devices.map(id => state.devices.get(id)?.name || id).join(", "));
  return parts.join(" + ") || "nothing";
}

function describeDays(days) {
  if (!days || days.length === 7) return "every day";
  const key = days.join(",");
  if (key === "mon,tue,wed,thu,fri") return "weekdays";
  if (key === "sat,sun") return "weekends";
  return days.map(d => d[0].toUpperCase() + d.slice(1)).join(" ");
}

function describeRoutine(r) {
  if (r.kind === "curve") {
    return `${describeTarget(r.target)} · ${r.points[0].at} → ${r.points[r.points.length - 1].at} · ${describeDays(r.days)}`;
  }
  const trig = r.triggers.map(t => t.type === "time" ? `at ${t.at}`
    : `when ${state.devices.get(t.device)?.name || t.device} ${t.attribute}${t.to !== null && t.to !== undefined ? ` → ${t.to}` : " changes"}`);
  const when = trig.length ? `${trig.join(" or ")} · ${describeDays(r.days)}` : "Run manually";
  const steps = r.actions.map(a => ({
    set: () => `set ${describeTarget(a.target)}`,
    command: () => `${a.command} ${describeTarget(a.target)}`,
    fade: () => `fade ${describeTarget(a.target)} over ${a.minutes} min`,
    delay: () => `wait ${a.minutes} min`,
    routine: () => `run ${a.routine}`,
  }[a.type]?.() || a.type));
  return `${when} — ${steps.join(", then ")}`;
}

function renderRoutines() {
  const list = $("#routine-list");
  if (!state.routines.length) {
    list.replaceChildren(h("div", { class: "empty" }, "No routines yet."));
    return;
  }
  list.replaceChildren(...state.routines.map(r => h("div", { class: "routine" + (r.enabled ? "" : " disabled") },
    h("div", { class: "routine-main" },
      h("div", { class: "routine-name" }, r.name,
        h("span", { class: "badge " + r.kind }, r.kind === "curve" ? "curve" : r.triggers.length ? "routine" : "scene"),
        r.running ? h("span", { class: "badge running" }, "running") : null),
      h("div", { class: "routine-summary" }, describeRoutine(r)),
      r.description ? h("div", { class: "routine-summary" }, r.description) : null,
      r.last_run ? h("div", { class: "routine-summary" },
        `Last run ${r.last_run.replace("T", " ").replace(/:\d\d[+-]\d\d:\d\d/, "")}`) : null),
    r.kind === "curve" ? miniCurve(r) : null,
    h("div", { class: "routine-actions" },
      h("label", { class: "switch", title: r.enabled ? "Disable" : "Enable" },
        h("input", { type: "checkbox", checked: r.enabled, onchange: e => saveRoutine({ ...stripMeta(r), enabled: e.target.checked }, r.id) }),
        h("span")),
      r.running
        ? h("button", { class: "btn small", onclick: () => api("POST", `/api/routines/${r.id}/stop`).then(loadRoutines) }, "Stop")
        : h("button", { class: "btn small", onclick: () => runRoutine(r) }, r.kind === "curve" ? "Apply now" : "Run"),
      h("button", { class: "btn small", onclick: () => openEditor(stripMeta(r), r.id) }, "Edit"),
      h("button", { class: "btn small danger", onclick: () => deleteRoutine(r) }, "Delete")),
  )));
}

function stripMeta(r) {
  const { last_run, running, ...rest } = r;
  return clone(rest);
}

async function runRoutine(r) {
  try {
    await api("POST", `/api/routines/${r.id}/run`);
    toast(`Running “${r.name}”`);
    setTimeout(loadRoutines, 400);
  } catch (e) { toast(e.message, true); }
}

async function deleteRoutine(r) {
  if (!confirm(`Delete “${r.name}”?`)) return;
  try { await api("DELETE", `/api/routines/${r.id}`); loadRoutines(); } catch (e) { toast(e.message, true); }
}

async function saveRoutine(routine, existingId) {
  try {
    if (existingId) await api("PUT", `/api/routines/${existingId}`, routine);
    else await api("POST", "/api/routines", routine);
    await loadRoutines();
    return true;
  } catch (e) {
    toast(e.message, true);
    return e;
  }
}

function miniCurve(r) {
  const svg = svgEl("svg", { class: "mini-curve", viewBox: "0 0 160 40", preserveAspectRatio: "none" });
  api("POST", "/api/curve/preview", { points: r.points }).then(samples => drawCurve(svg, samples, 160, 40, false)).catch(() => {});
  return svg;
}

// ---------------------------------------------------------------- curve chart

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function drawCurve(svg, samples, W, H, axes = true) {
  svg.replaceChildren();
  if (axes && svg.clientWidth) {
    W = svg.clientWidth; // draw at real pixel size so labels aren't stretched
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  }
  const pad = axes ? { l: 38, r: 14, t: 10, b: 18 } : { l: 0, r: 0, t: 2, b: 2 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const x = i => pad.l + (i / (samples.length - 1)) * iw;
  const y = v => pad.t + ih - (v / 100) * ih;
  // Colour-temperature band along the bottom.
  const band = axes ? 8 : 4;
  samples.forEach((s, i) => {
    if (s.color_temp == null || i === samples.length - 1) return;
    svg.append(svgEl("rect", { x: x(i), y: pad.t + ih - band, width: Math.max(1, x(i + 1) - x(i) + 0.5), height: band,
      fill: kelvinToRgb(s.color_temp) }));
  });
  if (axes) {
    for (const v of [0, 50, 100]) {
      svg.append(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), stroke: "currentColor", "stroke-opacity": 0.1 }));
      const t = svgEl("text", { x: pad.l - 4, y: y(v) + 3, "text-anchor": "end" }); t.textContent = `${v}%`; svg.append(t);
    }
    samples.forEach((s, i) => {
      const hour = +s.t.slice(11, 13), min = s.t.slice(14, 16);
      if (min === "00" && hour % 3 === 0) {
        const t = svgEl("text", { x: x(i), y: H - 4, "text-anchor": "middle" });
        t.textContent = `${hour}:00`;
        svg.append(t);
      }
    });
    const now = state.status ? new Date(state.status.now) : new Date();
    const first = new Date(samples[0].t), last = new Date(samples[samples.length - 1].t);
    if (now >= first && now <= last) {
      const nx = pad.l + ((now - first) / (last - first)) * iw;
      svg.append(svgEl("line", { x1: nx, x2: nx, y1: pad.t, y2: pad.t + ih, stroke: "#3d8be8", "stroke-dasharray": "3 3" }));
    }
  }
  // Brightness line, broken where the curve is inactive.
  let d = "", pen = false;
  samples.forEach((s, i) => {
    if (s.brightness == null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(s.brightness).toFixed(1)}`;
    pen = true;
  });
  svg.append(svgEl("path", { d, fill: "none", stroke: "#e8a33d", "stroke-width": axes ? 2.5 : 2, "stroke-linejoin": "round" }));
}

// ---------------------------------------------------------------- routine editor

const editor = {
  routine: null, existingId: null, jsonMode: false, previewTimer: null,
};

const TEMPLATES = {
  curve: () => ({
    kind: "curve", id: "", name: "", enabled: true, days: [...DAYS], description: "",
    target: { kinds: ["light"], rooms: [], devices: [], all: false },
    points: [
      { at: "sunset-60", brightness: 100, color_temp: 4500 },
      { at: "21:00", brightness: 50, color_temp: 2700 },
      { at: "22:30", brightness: 15, color_temp: 2200 },
    ],
    only_when_on: true, manual_override_minutes: 60, step_minutes: 2,
  }),
  event: () => ({
    kind: "event", id: "", name: "", enabled: true, days: [...DAYS], description: "",
    triggers: [{ type: "time", at: "07:00" }], between: null,
    actions: [{ type: "set", target: { kinds: ["light"], rooms: [], devices: [], all: false }, state: { on: true } }],
  }),
};

const ACTION_TEMPLATES = {
  set: () => ({ type: "set", target: { kinds: ["light"], rooms: [], devices: [], all: false }, state: { on: true } }),
  fade: () => ({ type: "fade", target: { kinds: ["light"], rooms: [], devices: [], all: false }, minutes: 15, brightness: 20, color_temp: 2200, off_at_end: false }),
  command: () => ({ type: "command", target: { kinds: ["vacuum"], rooms: [], devices: [], all: false }, command: "start" }),
  delay: () => ({ type: "delay", minutes: 5 }),
  routine: () => ({ type: "routine", routine: state.routines[0]?.id || "" }),
};

document.querySelectorAll("[data-new]").forEach(b => b.addEventListener("click", () => openEditor(TEMPLATES[b.dataset.new](), null)));
$("#editor-cancel").addEventListener("click", () => $("#editor").close());
$("#editor-json-toggle").addEventListener("click", () => toggleJson());
$("#editor-form").addEventListener("submit", async e => {
  e.preventDefault();
  if (editor.jsonMode && !readJson()) return;
  const r = clone(editor.routine);
  if (!r.name.trim()) { $("#editor-errors").textContent = "Give the routine a name."; return; }
  if (!editor.existingId) r.id = r.id || uniqueId(slug(r.name));
  const result = await saveRoutine(r, editor.existingId);
  if (result === true) $("#editor").close();
  else $("#editor-errors").textContent = result.message;
});

function uniqueId(base) {
  let id = base, n = 2;
  while (state.routines.some(r => r.id === id)) id = `${base}-${n++}`;
  return id;
}

function openEditor(routine, existingId) {
  editor.routine = routine;
  editor.existingId = existingId;
  editor.jsonMode = false;
  $("#editor-title").textContent = (existingId ? "Edit " : "New ") + (routine.kind === "curve" ? "lighting curve" : "routine");
  $("#editor-errors").textContent = "";
  $("#editor-json").classList.add("hidden");
  $("#editor-body").classList.remove("hidden");
  renderEditor();
  $("#editor").showModal();
}

function toggleJson() {
  if (editor.jsonMode) {
    if (!readJson()) return;
    editor.jsonMode = false;
    renderEditor();
  } else {
    editor.jsonMode = true;
    $("#editor-json").value = JSON.stringify(editor.routine, null, 2);
  }
  $("#editor-json").classList.toggle("hidden", !editor.jsonMode);
  $("#editor-body").classList.toggle("hidden", editor.jsonMode);
}

function readJson() {
  try {
    editor.routine = JSON.parse($("#editor-json").value);
    $("#editor-errors").textContent = "";
    return true;
  } catch (e) {
    $("#editor-errors").textContent = `Invalid JSON: ${e.message}`;
    return false;
  }
}

function field(label, control, hint) {
  return h("div", { class: "field" }, h("label", {}, label), control, hint ? h("div", { class: "hint" }, hint) : null);
}

function textInput(obj, key, attrs = {}, onChange) {
  return h("input", { type: "text", value: obj[key] ?? "", ...attrs,
    oninput: e => { obj[key] = e.target.value; onChange?.(); } });
}

function numInput(obj, key, attrs = {}, onChange) {
  return h("input", { type: "number", value: obj[key] ?? "", ...attrs,
    oninput: e => { obj[key] = e.target.value === "" ? null : +e.target.value; onChange?.(); } });
}

function checkbox(obj, key, label, onChange) {
  return h("label", { class: "checkbox" },
    h("input", { type: "checkbox", checked: obj[key], onchange: e => { obj[key] = e.target.checked; onChange?.(); } }), label);
}

function daysPicker(r) {
  return h("div", { class: "chips" }, DAYS.map(d => h("button", {
    type: "button", class: "chip" + (r.days.includes(d) ? " active" : ""),
    onclick: e => {
      r.days = r.days.includes(d) ? r.days.filter(x => x !== d) : DAYS.filter(x => x === d || r.days.includes(x));
      e.target.classList.toggle("active");
    },
  }, d[0].toUpperCase() + d.slice(1))));
}

function targetPicker(target) {
  for (const k of ["kinds", "rooms", "devices"]) target[k] = target[k] || [];
  const wrap = h("div", { class: "target" });
  const render = () => {
    const group = (label, key, options) => h("div", {},
      h("div", { class: "target-label" }, label),
      h("div", { class: "chips" }, options.map(([value, text]) => h("button", {
        type: "button", class: "chip" + (target[key].includes(value) ? " active" : ""),
        onclick: () => {
          target[key] = target[key].includes(value) ? target[key].filter(x => x !== value) : [...target[key], value];
          target.all = false;
          render();
        },
      }, text))));
    const kinds = [...new Set(devicesSorted().map(d => d.kind))];
    wrap.replaceChildren(
      h("label", { class: "checkbox" }, h("input", { type: "checkbox", checked: target.all,
        onchange: e => { target.all = e.target.checked; render(); } }), "Every device"),
      target.all ? null : group("All devices of type", "kinds", kinds.map(k => [k, KIND_LABELS[k] || k])),
      target.all ? null : group("Rooms", "rooms", rooms().map(r => [r, r])),
      target.all ? null : group("Specific devices", "devices", visibleDevices().map(d => [d.id, d.name])),
    );
  };
  render();
  return wrap;
}

function renderEditor() {
  const r = editor.routine;
  const body = $("#editor-body");
  const common = [
    field("Name", textInput(r, "name", { placeholder: r.kind === "curve" ? "Evening wind-down" : "Morning", required: true })),
    field("Description", textInput(r, "description", { placeholder: "Optional" })),
    field("Days", daysPicker(r)),
  ];
  body.replaceChildren(...common, ...(r.kind === "curve" ? curveEditor(r) : eventEditor(r)));
  if (r.kind === "curve") schedulePreview();
}

// ---- curve editor

function curveEditor(r) {
  const tbody = h("tbody");
  const chart = svgEl("svg", { class: "chart", viewBox: "0 0 700 170", preserveAspectRatio: "none" });
  editor.chart = chart;
  const renderPoints = () => {
    tbody.replaceChildren(...r.points.map((p, i) => h("tr", {},
      h("td", {}, textInput(p, "at", { placeholder: "21:00 or sunset+30" }, schedulePreview)),
      h("td", {}, numInput(p, "brightness", { min: 0, max: 100, placeholder: "–" }, schedulePreview)),
      h("td", {}, numInput(p, "color_temp", { min: 1500, max: 9000, step: 100, placeholder: "–" }, () => { schedulePreview(); renderSwatches(); })),
      h("td", {}, h("span", { class: "swatch", "data-i": i })),
      h("td", {}, h("button", { type: "button", class: "btn small ghost", disabled: r.points.length <= 2,
        onclick: () => { r.points.splice(i, 1); renderPoints(); schedulePreview(); } }, "✕")))));
    renderSwatches();
  };
  const renderSwatches = () => tbody.querySelectorAll(".swatch").forEach(s => {
    const ct = r.points[+s.dataset.i].color_temp;
    s.style.background = ct ? kelvinToRgb(ct) : "transparent";
  });
  renderPoints();
  return [
    field("Lights", targetPicker(r.target)),
    field("Keyframes",
      h("div", {},
        h("table", { class: "points" },
          h("thead", {}, h("tr", {}, h("th", {}, "Time"), h("th", {}, "Brightness %"), h("th", {}, "Colour temp K"), h("th"), h("th"))),
          tbody),
        h("button", { type: "button", class: "btn small", onclick: () => {
          const last = r.points[r.points.length - 1];
          r.points.push({ at: "23:00", brightness: last?.brightness ?? 10, color_temp: last?.color_temp ?? 2200 });
          renderPoints(); schedulePreview();
        } }, "+ Keyframe"),
        chart),
      "Times are HH:MM or sun-relative (sunrise, sunset, dusk, dawn ± minutes, e.g. sunset-45). " +
      "Keyframes run in order, so a later keyframe at an earlier clock time means the next morning. " +
      "2000K is candle-warm, 2700K a classic bulb, 4000K neutral, 5000K+ daylight."),
    field("Options", h("div", { class: "inline" },
      checkbox(r, "only_when_on", "Only adjust lights that are already on"),
    )),
    field("Pause after a manual change (minutes)", numInput(r, "manual_override_minutes", { min: 0 }),
      "If you set a light's brightness by hand, the curve leaves that light alone for this long."),
    field("Update every (minutes)", numInput(r, "step_minutes", { min: 0.5, max: 60, step: 0.5 })),
  ];
}

function schedulePreview() {
  clearTimeout(editor.previewTimer);
  editor.previewTimer = setTimeout(async () => {
    try {
      const samples = await api("POST", "/api/curve/preview", { points: editor.routine.points });
      drawCurve(editor.chart, samples, 700, 170);
      $("#editor-errors").textContent = "";
    } catch (e) {
      $("#editor-errors").textContent = e.message;
    }
  }, 250);
}

// ---- event editor

function eventEditor(r) {
  const triggers = h("div");
  const actions = h("div");

  const renderTriggers = () => triggers.replaceChildren(...r.triggers.map((t, i) => {
    const typeSel = h("select", { onchange: e => {
      r.triggers[i] = e.target.value === "time" ? { type: "time", at: "07:00" }
        : { type: "device", device: devicesSorted()[0]?.id || "", attribute: "on", to: true };
      renderTriggers();
    } }, h("option", { value: "time", selected: t.type === "time" }, "At a time"),
       h("option", { value: "device", selected: t.type === "device" }, "When a device changes"));
    const remove = h("button", { type: "button", class: "btn small ghost", onclick: () => { r.triggers.splice(i, 1); renderTriggers(); } }, "✕");
    let fields;
    if (t.type === "time") {
      fields = [textInput(t, "at", { placeholder: "07:00 or sunset-30" })];
    } else {
      fields = [
        h("select", { onchange: e => { t.device = e.target.value; } },
          devicesSorted().map(d => h("option", { value: d.id, selected: d.id === t.device }, d.name))),
        textInput(t, "attribute", { placeholder: "on / status / ..." , style: "width:110px" }),
        h("span", { class: "muted" }, "becomes"),
        h("input", { type: "text", value: t.to === null || t.to === undefined ? "" : JSON.stringify(t.to),
          placeholder: "anything", style: "width:110px",
          oninput: e => { t.to = parseLoose(e.target.value); } }),
      ];
    }
    return h("div", { class: "block" }, h("div", { class: "block-head" }, typeSel, ...fields, h("div", { class: "spacer" }), remove));
  }));

  const renderActions = () => actions.replaceChildren(...r.actions.map((a, i) => actionBlock(r, a, i, renderActions)));
  renderTriggers();
  renderActions();

  const between = r.between || ["", ""];
  const setBetween = () => { r.between = between[0] && between[1] ? [between[0], between[1]] : null; };

  return [
    field("Triggers", h("div", {}, triggers,
      h("div", { class: "inline", style: "margin-top:8px" },
        h("button", { type: "button", class: "btn small", onclick: () => { r.triggers.push({ type: "time", at: "07:00" }); renderTriggers(); } }, "+ Trigger"),
        h("span", { class: "hint" }, "No triggers = a scene you run by hand."))),
      "Times are HH:MM or sunrise / sunset / dusk / dawn ± minutes (e.g. sunset-30)."),
    field("Only between (optional)", h("div", { class: "inline" },
      h("input", { type: "text", value: between[0], placeholder: "sunset", oninput: e => { between[0] = e.target.value; setBetween(); } }),
      h("span", { class: "muted" }, "and"),
      h("input", { type: "text", value: between[1], placeholder: "23:00", oninput: e => { between[1] = e.target.value; setBetween(); } })),
      "Useful with device triggers, e.g. only turn the hall light on when the door opens after dark."),
    field("Steps (run in order)", h("div", {}, actions,
      h("div", { class: "inline", style: "margin-top:8px" },
        ...Object.keys(ACTION_TEMPLATES).map(type => h("button", { type: "button", class: "btn small",
          onclick: () => { r.actions.push(ACTION_TEMPLATES[type]()); renderActions(); } }, `+ ${actionLabel(type)}`))))),
  ];
}

function actionLabel(type) {
  return { set: "Set", fade: "Fade", command: "Command", delay: "Wait", routine: "Run routine" }[type];
}

function parseLoose(text) {
  if (text.trim() === "") return null;
  try { return JSON.parse(text); } catch { return text; }
}

function actionBlock(r, a, i, rerender) {
  const move = delta => {
    const j = i + delta;
    if (j < 0 || j >= r.actions.length) return;
    [r.actions[i], r.actions[j]] = [r.actions[j], r.actions[i]];
    rerender();
  };
  const head = h("div", { class: "block-head" },
    h("b", {}, `${i + 1}. ${actionLabel(a.type)}`),
    h("div", { class: "spacer" }),
    h("button", { type: "button", class: "btn small ghost", onclick: () => move(-1), title: "Move up" }, "↑"),
    h("button", { type: "button", class: "btn small ghost", onclick: () => move(1), title: "Move down" }, "↓"),
    h("button", { type: "button", class: "btn small ghost", disabled: r.actions.length <= 1,
      onclick: () => { r.actions.splice(i, 1); rerender(); } }, "✕"));
  const block = h("div", { class: "block" }, head);

  if (a.type === "set") {
    block.append(targetPicker(a.target), stateFields(a.state));
  } else if (a.type === "fade") {
    block.append(targetPicker(a.target), h("div", { class: "inline" },
      "Over", numInput(a, "minutes", { min: 0.5, step: 0.5 }), "min to",
      numInput(a, "brightness", { min: 0, max: 100, placeholder: "–" }), "% and",
      numInput(a, "color_temp", { min: 1500, max: 9000, step: 100, placeholder: "–" }), "K",
      checkbox(a, "off_at_end", "then turn off")));
  } else if (a.type === "command") {
    block.append(targetPicker(a.target), h("div", { class: "inline" }, "Command",
      h("select", { onchange: e => { a.command = e.target.value; } },
        VACUUM_CMDS.map(c => h("option", { value: c, selected: a.command === c }, c)))));
  } else if (a.type === "delay") {
    block.append(h("div", { class: "inline" }, "Wait", numInput(a, "minutes", { min: 0.5, step: 0.5 }), "minutes"));
  } else if (a.type === "routine") {
    block.append(h("select", { onchange: e => { a.routine = e.target.value; } },
      state.routines.filter(x => x.id !== r.id).map(x => h("option", { value: x.id, selected: a.routine === x.id }, x.name))));
  }
  return block;
}

function stateFields(s) {
  const opt = (key, label, control) => {
    const enabled = key in s;
    const box = h("input", { type: "checkbox", checked: enabled, onchange: e => {
      if (e.target.checked) s[key] = control.defaultValue(); else delete s[key];
      wrap.replaceWith(stateFields(s));
    } });
    return h("label", { class: "checkbox" }, box, label, enabled ? control.el() : null);
  };
  const tempDefault = unit() === "°F" ? 68 : 20;
  const wrap = h("div", { class: "inline" },
    opt("on", "Power", { defaultValue: () => true, el: () => h("select", { onchange: e => { s.on = e.target.value === "true"; } },
      h("option", { value: "true", selected: s.on === true }, "on"), h("option", { value: "false", selected: s.on === false }, "off")) }),
    opt("brightness", "Brightness", { defaultValue: () => 50, el: () => numInput(s, "brightness", { min: 0, max: 100 }) }),
    opt("color_temp", "Colour temp", { defaultValue: () => 2700, el: () => numInput(s, "color_temp", { min: 1500, max: 9000, step: 100 }) }),
    opt("target_temp", "Thermostat", { defaultValue: () => tempDefault, el: () => numInput(s, "target_temp", { step: 0.5 }) }),
    opt("mode", "Mode", { defaultValue: () => "cool", el: () => h("select", { onchange: e => { s.mode = e.target.value; } },
      MODES.map(m => h("option", { value: m, selected: s.mode === m }, m))) }),
  );
  return wrap;
}

// ---------------------------------------------------------------- settings view

function renderSettings() {
  const st = state.status;
  if (!st) return;
  $("#integration-list").replaceChildren(
    ...st.integrations.map(i => h("div", { class: "integration" },
      h("span", { class: "dot" + (i.error ? " err" : "") }),
      h("b", {}, i.id), h("span", { class: "muted" }, i.type),
      h("div", { class: "spacer" }),
      i.error ? h("span", { class: "errors", style: "padding:0" }, i.error) : h("span", { class: "muted" }, `${i.devices} devices`))),
    st.location_configured ? null : h("p", { class: "muted" },
      "Tip: add your location to config.yaml so sunrise/sunset times are accurate (currently using 6:30 / 18:30)."));

  const roomList = h("datalist", { id: "room-options" }, rooms().map(r => h("option", { value: r })));
  $("#device-settings").replaceChildren(roomList, ...devicesSorted().map(d => {
    const name = h("input", { type: "text", value: d.name });
    const room = h("input", { type: "text", value: d.room || "", list: "room-options", placeholder: "Room" });
    const hidden = h("input", { type: "checkbox", checked: d.attributes.hidden });
    return h("div", { class: "settings-row" },
      name, room,
      h("span", { class: "muted" }, `${ICONS[d.kind] || ""} ${d.integration}`),
      h("div", { class: "inline" },
        h("label", { class: "checkbox" }, hidden, "Hide"),
        h("button", { class: "btn small", onclick: async () => {
          try {
            upsertDevice(await api("PATCH", `/api/devices/${encodeURIComponent(d.id)}`,
              { name: name.value, room: room.value, hidden: hidden.checked }));
            toast("Saved");
          } catch (e) { toast(e.message, true); }
        } }, "Save")));
  }));
}

// ---------------------------------------------------------------- clock & live updates

function renderClock() {
  const st = state.status;
  const now = new Date();
  const tz = st?.timezone;
  const fmt = (d, opts) => d.toLocaleTimeString([], { timeZone: tz, hour: "numeric", minute: "2-digit", ...opts });
  $("#clock-time").textContent = fmt(now);
  if (st) $("#clock-sun").textContent = `☀ ${fmt(new Date(st.sun.sunrise))}  ☾ ${fmt(new Date(st.sun.sunset))}`;
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  let ping;
  ws.onopen = () => { ping = setInterval(() => ws.send("ping"), 25000); refresh(); };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === "device") upsertDevice(msg.device);
  };
  ws.onclose = () => { clearInterval(ping); setTimeout(connect, 3000); };
}

async function refresh() {
  try {
    const [status, devices] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/devices")]);
    state.status = status;
    state.devices = new Map(devices.map(d => [d.id, d]));
    renderDevices();
    renderClock();
    const view = localGet("view") || "devices";
    if (view === "settings") renderSettings();
    if (view === "routines") loadRoutines();
  } catch (e) { toast(`Can't reach the hub: ${e.message}`, true); }
}

showView(localGet("view") || "devices");
refresh();
connect();
setInterval(renderClock, 15000);
setInterval(async () => { try { state.status = await api("GET", "/api/status"); } catch { /* offline */ } }, 10 * 60000);
