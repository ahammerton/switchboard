"use strict";

const $ = (sel) => document.querySelector(sel);
const ICON_CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/";
const WEATHER_REFRESH_MS = 15 * 60 * 1000;
const RSS_REFRESH_MS = 10 * 60 * 1000;

let cfg = null;
let meta = { editProtected: false };
let activeTab = 0;
let statusTimer = null;
let lastStatus = {};
let lastWidgets = {};  // service id -> { fields } | { error }
let widgetPresets = {}; // preset id -> { name, group, options, help }
let dragSrc = null; // { ti, gi, si }
let rssData = null;   // feed url -> { title, link, items } | { error }
let rssFetchedAt = 0;
let rssLoading = false;

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c != null) node.append(c instanceof Node ? c : document.createTextNode(c));
  }
  return node;
}

const isEditing = () => document.body.classList.contains("editing");
const rssOn = () => !!cfg.rss?.enabled;
const upcomingOn = () => !!cfg.upcoming?.enabled;

/** The configured tabs, then the built-in ones that are switched on (Upcoming, News). */
function allTabs() {
  const tabs = cfg.tabs.map((t) => ({ name: t.name, icon: t.icon }));
  if (upcomingOn()) tabs.push({ name: cfg.upcoming.tabName || "Upcoming", icon: cfg.upcoming.icon ?? "📅", special: "upcoming" });
  if (rssOn()) tabs.push({ name: cfg.rss.tabName || "News", icon: cfg.rss.icon ?? "📰", special: "rss" });
  return tabs;
}

const specialAt = (ti) => (ti >= cfg.tabs.length ? allTabs()[ti]?.special ?? null : null);
const isRssTab = (ti) => specialAt(ti) === "rss";
const isUpcomingTab = (ti) => specialAt(ti) === "upcoming";

const slug = (s) => (s || "").toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

function safeUrl(url) {
  return /^(https?:|\/|\.)/i.test(url || "") ? url : "#";
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}

function renderIcon(icon, name) {
  const box = el("span", { class: "tile-icon" });
  const fallback = () => { box.textContent = (name || "?").charAt(0).toUpperCase(); };
  if (!icon) {
    fallback();
  } else if (icon.startsWith("di:")) {
    box.append(el("img", { src: ICON_CDN + encodeURIComponent(icon.slice(3)) + ".png", alt: "", loading: "lazy", onerror: fallback }));
  } else if (/^(https?:|\/|data:image\/)/i.test(icon)) {
    box.append(el("img", { src: icon, alt: "", loading: "lazy", onerror: fallback }));
  } else {
    box.textContent = icon; // emoji or short text
  }
  return box;
}

/** Older configs had top-level groups; wrap them in a single tab. */
function normaliseConfig(c) {
  if (!Array.isArray(c.tabs)) {
    if (!Array.isArray(c.groups)) throw new Error("config needs a “tabs” (or “groups”) array");
    c.tabs = [{ name: "Home", groups: c.groups }];
    delete c.groups;
  }
  if (!c.tabs.length) c.tabs.push({ name: "Home", groups: [] });
  for (const t of c.tabs) {
    t.groups ??= [];
    for (const g of t.groups) g.services ??= [];
  }
  c.rss ??= { enabled: false, feeds: [] };
  c.rss.feeds ??= [];
  return c;
}

// ---------- API ----------

async function api(method, path, body, extraHeaders = {}) {
  const raw = body instanceof Blob; // file uploads are sent as-is
  const headers = { ...(raw ? {} : { "Content-Type": "application/json" }), ...extraHeaders };
  const token = localStorage.getItem("editToken");
  if (token) headers["X-Edit-Token"] = token;
  const res = await fetch(path, { method, headers, body: raw ? body : body && JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) localStorage.removeItem("editToken");
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { status: res.status });
  return data;
}

/** Ask for the edit token (once) when the server requires one for changes. */
function ensureToken() {
  if (meta.editProtected && !localStorage.getItem("editToken")) {
    const t = prompt("Edit token required to make changes.\n\nIt's printed in the server log on first start and saved in the config folder as edit-token.");
    if (!t) throw new Error("No edit token given");
    localStorage.setItem("editToken", t);
  }
}

async function save() {
  try {
    ensureToken();
    await api("PUT", "/api/config", cfg);
    toast("Saved");
    render();
    refreshStatus();
    refreshWidgets();
    refreshWeather();
    loadRss("reload");
    loadUpcoming("reload");
  } catch (e) {
    toast(e.message, true);
    render();
  }
}

// ---------- rendering ----------

/** "glass" or "normal": this device's choice if it made one, else the dashboard default. */
function uiStyle() {
  let local = null;
  try { local = localStorage.getItem("uiStyle"); } catch { /* storage unavailable */ }
  return local || cfg.theme?.style || "normal";
}

function toggleUiStyle() {
  const next = uiStyle() === "glass" ? "normal" : "glass";
  try {
    if (next === (cfg.theme?.style || "normal")) localStorage.removeItem("uiStyle");
    else localStorage.setItem("uiStyle", next);
  } catch { /* storage unavailable: applies until reload */ }
  applyTheme(next);
  toast(next === "glass" ? "Glass interface" : "Normal interface");
}

function setBackground(url) {
  const bg = $("#bg");
  bg.style.backgroundImage = url ? `url("${url.replace(/"/g, "%22")}")` : "";
  bg.classList.toggle("has-image", !!url);
}

function applyTheme(style = uiStyle()) {
  const theme = cfg.theme || {};
  const root = document.documentElement;
  root.dataset.ui = style;
  const btn = $("#uiToggle");
  btn.setAttribute("aria-pressed", String(style === "glass"));
  btn.title = `Switch to ${style === "glass" ? "normal" : "glass"} interface on this device (G)`;
  root.style.setProperty("--accent", theme.accent || "#6366f1");
  if (theme.mode === "light" || theme.mode === "dark") root.dataset.theme = theme.mode;
  else delete root.dataset.theme;
  root.dataset.tile = theme.tileSize || "medium";
  setBackground(theme.background);
}

function render() {
  const tabs = allTabs();
  activeTab = Math.min(activeTab, tabs.length - 1);
  const tab = tabs[activeTab];
  applyTheme();
  document.title = tabs.length > 1 ? `${tab.name} · ${cfg.title || "Switchboard"}` : (cfg.title || "Switchboard");
  history.replaceState(null, "", tabs.length > 1 ? "#" + slug(tab.name) : location.pathname);

  $("#title").textContent = cfg.title || "";
  $("#subtitle").textContent = cfg.subtitle || "";
  $("#clock").classList.toggle("hidden", cfg.showClock === false);
  $("#greeting").classList.toggle("hidden", cfg.showGreeting === false);
  $("#search").classList.toggle("hidden", cfg.search?.enabled === false);
  $("#weather").classList.toggle("hidden", !cfg.weather?.enabled);
  renderSystem(lastSystem);
  renderTabs();

  // While searching, show matches from every tab, not just the active one.
  const searching = !!$("#searchInput").value.trim();
  const root = $("#groups");
  root.replaceChildren();
  if (tab.special === "rss" && !searching) return renderRss(root);
  if (tab.special === "upcoming" && !searching) return renderUpcoming(root);
  cfg.tabs.forEach((t, ti) => {
    if (!searching && ti !== activeTab) return;
    t.groups.forEach((group, gi) => root.append(renderGroup(group, ti, gi, searching && cfg.tabs.length > 1)));
  });

  applyFilter();
  if (!searching && !cfg.tabs[activeTab].groups.length) {
    root.append(el("p", { class: "empty" }, "This tab is empty — click ✏️ then “New Group” to add some services."));
  } else if (searching && !root.querySelector(".tile[data-search]:not(.hidden)")) {
    root.append(el("p", { class: "empty" }, "No services match — press Enter to search the web."));
  }
  applyStatus(lastStatus);
  applyWidgets(lastWidgets);
  applyHistory(lastHistory);
}

function renderTabs() {
  const bar = $("#tabs");
  bar.replaceChildren();
  const tabs = allTabs();
  bar.classList.toggle("hidden", tabs.length < 2 && !isEditing());

  tabs.forEach((tab, ti) => {
    const active = ti === activeTab;
    const btn = el("button", {
      class: "tab" + (active ? " active" : ""),
      type: "button",
      role: "tab",
      "aria-selected": String(active),
      title: ti < 9 ? `${tab.name} (${ti + 1})` : tab.name,
      onclick: () => selectTab(ti),
    }, tab.icon ? `${tab.icon} ` : null, tab.name || "Untitled");

    // Drop a tile on a tab to move the service there.
    btn.addEventListener("dragover", (e) => {
      if (dragSrc && dragSrc.ti !== ti && !tab.special) { e.preventDefault(); btn.classList.add("drop-target"); }
    });
    btn.addEventListener("dragleave", () => btn.classList.remove("drop-target"));
    btn.addEventListener("drop", (e) => { e.preventDefault(); moveServiceToTab(dragSrc, ti); });
    bar.append(btn);
  });

  const newButtons = [
    el("button", { class: "btn small", type: "button", onclick: () => editTab() }, "New Tab"),
    el("button", {
      class: "btn small", type: "button", onclick: () => editGroup(),
      title: specialAt(activeTab) ? `Adds a group to “${cfg.tabs[0].name}” (built-in tabs don't hold groups)` : null,
    }, "New Group"),
    el("button", { class: "btn small", type: "button", onclick: findServices,
      title: "Scan your machines for apps that aren't on the dashboard yet" }, "Find services"),
  ];
  if (specialAt(activeTab)) {
    bar.append(el("span", { class: "edit-only tab-tools" },
      el("button", { class: "btn small", type: "button", onclick: isRssTab(activeTab) ? editRssOptions : editUpcomingOptions }, "Edit tab"),
      ...newButtons));
    return;
  }
  const last = cfg.tabs.length - 1;
  bar.append(el("span", { class: "edit-only tab-tools" },
    el("button", { class: "btn small", type: "button", onclick: () => editTab(activeTab) }, "Edit tab"),
    el("button", { class: "btn small", type: "button", title: "Move tab left", disabled: activeTab === 0, onclick: () => moveTab(activeTab, -1) }, "←"),
    el("button", { class: "btn small", type: "button", title: "Move tab right", disabled: activeTab === last, onclick: () => moveTab(activeTab, 1) }, "→"),
    ...newButtons,
  ));
}

function selectTab(ti) {
  activeTab = ti;
  try { localStorage.setItem("activeTab", slug(allTabs()[ti].name)); } catch { /* storage unavailable */ }
  if ($("#searchInput").value) $("#searchInput").value = "";
  render();
  if (isRssTab(ti)) loadRss();
  if (isUpcomingTab(ti)) loadUpcoming();
}

function tabIndexBySlug(s) {
  return s ? allTabs().findIndex((t) => slug(t.name) === s) : -1;
}

function renderGroup(group, ti, gi, showTabName) {
  const grid = el("div", { class: "grid" });
  grid.addEventListener("dragover", (e) => { if (dragSrc) { e.preventDefault(); grid.classList.add("drop-target"); } });
  grid.addEventListener("dragleave", () => grid.classList.remove("drop-target"));
  grid.addEventListener("drop", (e) => {
    e.preventDefault();
    grid.classList.remove("drop-target");
    moveService(dragSrc, ti, gi, group.services.length);
  });

  group.services.forEach((svc, si) => grid.append(renderTile(svc, ti, gi, si)));
  grid.append(el("button", { class: "tile add-tile edit-only", type: "button", onclick: () => editService(ti, gi) }, "+ Add service"));

  const last = cfg.tabs[ti].groups.length - 1;
  return el("section", { class: "group" },
    el("div", { class: "group-head" },
      el("h2", {},
        showTabName ? el("span", { class: "group-tab" }, `${cfg.tabs[ti].name} › `) : null,
        group.name || "Untitled"),
      el("span", { class: "edit-only", style: "gap:4px" },
        el("button", { class: "btn small", title: "Edit group", onclick: () => editGroup(ti, gi) }, "Edit"),
        el("button", { class: "btn small", title: "Move up", disabled: gi === 0, onclick: () => moveGroup(ti, gi, -1) }, "↑"),
        el("button", { class: "btn small", title: "Move down", disabled: gi === last, onclick: () => moveGroup(ti, gi, 1) }, "↓"),
      ),
    ),
    grid,
  );
}

function renderTile(svc, ti, gi, si) {
  const tile = el("a", {
    class: "tile" + (svc.widget?.type ? " has-widget" : ""),
    href: safeUrl(svc.url),
    target: cfg.openInNewTab === false ? null : "_blank",
    rel: "noopener noreferrer",
    title: svc.url,
    draggable: isEditing() ? "true" : null,
    "data-search": `${svc.name} ${svc.description || ""} ${svc.url}`.toLowerCase(),
    "data-status-url": svc.check === false ? null : (svc.statusUrl || svc.url),
    "data-widget-id": svc.widget?.type && svc.id ? svc.id : null,
  },
    renderIcon(svc.icon, svc.name),
    el("span", { class: "tile-text" },
      el("div", { class: "tile-name" }, svc.name || "Untitled"),
      svc.description ? el("div", { class: "tile-desc" }, svc.description) : null,
    ),
    svc.check === false ? null : el("span", { class: "status", title: "Checking…" }),
    svc.widget?.type ? el("div", { class: "tile-stats" }) : null,
  );

  tile.addEventListener("click", (e) => {
    if (isEditing()) { e.preventDefault(); editService(ti, gi, si); }
  });
  tile.addEventListener("dragstart", (e) => {
    if (!isEditing()) return e.preventDefault();
    dragSrc = { ti, gi, si };
    tile.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  tile.addEventListener("dragend", () => { tile.classList.remove("dragging"); dragSrc = null; });
  tile.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    tile.closest(".grid").classList.remove("drop-target");
    moveService(dragSrc, ti, gi, si);
  });
  return tile;
}

// ---------- status ----------

function applyStatus(data) {
  document.querySelectorAll(".tile[data-status-url]").forEach((tile) => {
    const dot = tile.querySelector(".status");
    const s = data[tile.dataset.statusUrl];
    if (!dot || !s) return;
    dot.className = "status " + (s.up ? "up" : "down");
    dot.title = s.up ? `Online · HTTP ${s.code} · ${s.ms} ms` : `Offline · ${s.error || "HTTP " + s.code}`;
  });
}

async function refreshStatus() {
  try {
    lastStatus = await api("GET", "/api/status");
    applyStatus(lastStatus);
  } catch { /* server unreachable; leave dots as-is */ }
}

function startStatusPolling() {
  clearInterval(statusTimer);
  refreshStatus();
  const secs = Math.max(10, Number(cfg.statusInterval) || 30);
  statusTimer = setInterval(refreshStatus, secs * 1000);
}

// ---------- service widgets ----------

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

function formatBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function formatDuration(sec) {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function formatStat(v, format) {
  if (v == null || v === "") return "—";
  if (typeof v !== "number" || format === "text") return String(v);
  switch (format) {
    case "bytes": return formatBytes(v);
    case "speed": return formatBytes(v) + "/s";
    case "percent": return `${v < 10 ? v.toFixed(1) : Math.round(v)}%`;
    case "duration": return formatDuration(v);
    case "ms": return `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`;
    default: return Math.abs(v) >= 10000 ? compact.format(v) : String(Math.round(v * 10) / 10);
  }
}

function renderStats(box, result) {
  box.classList.toggle("error", !!result.error);
  if (result.error) {
    box.title = result.error;
    box.replaceChildren(el("span", { class: "stat-error" }, "⚠️ " + result.error));
    return;
  }
  box.title = result.fields.map((f) => `${f.label}: ${formatStat(f.value, f.format)}`).join("\n");
  box.replaceChildren(...result.fields.map((f) => el("span", { class: "stat" + (f.warn ? " warn" : "") },
    el("span", { class: "stat-value" }, formatStat(f.value, f.format)),
    el("span", { class: "stat-label" }, f.label))));
}

function applyWidgets(data) {
  document.querySelectorAll(".tile[data-widget-id]").forEach((tile) => {
    const r = data[tile.dataset.widgetId];
    const box = tile.querySelector(".tile-stats");
    if (r && box) renderStats(box, r);
  });
}

async function refreshWidgets() {
  if (!cfg.tabs.some((t) => t.groups.some((g) => g.services.some((s) => s.widget?.type)))) return;
  try {
    lastWidgets = (await api("GET", "/api/widgets")).widgets;
    applyWidgets(lastWidgets);
  } catch { /* keep the last values */ }
}

// ---------- system stats ----------

const SYSTEM_METRICS = [["cpu", "CPU usage"], ["temp", "CPU temperature"], ["ram", "Free memory"],
  ["disk", "Free disk space"], ["load", "Load average"], ["uptime", "Uptime"]];
const DEFAULT_DISKS = [{ path: "/", label: "System" }];
let lastSystem = null;
let systemTimer = null;

const sysShown = (key) => cfg.system?.show?.[key] !== false;
const formatTemp = (c) => cfg.system?.tempUnit === "F" ? `${Math.round(c * 9 / 5 + 32)}°F` : `${Math.round(c)}°C`;

function sysItem(label, value, { pct, title, warn } = {}) {
  return el("div", { class: "sys-item" + (warn ? " warn" : ""), title },
    el("span", { class: "sys-label" }, label),
    el("span", { class: "sys-value" }, value),
    pct == null ? null : el("span", { class: "sys-meter" },
      el("span", { class: warn ? "warn" : null, style: `width:${Math.max(0, Math.min(100, pct)).toFixed(0)}%` })));
}

function renderSystem(d) {
  const bar = $("#sysbar");
  const sc = cfg.system || {};
  bar.classList.toggle("hidden", !sc.enabled);
  if (!sc.enabled) return;
  const warnPct = Number(sc.warnPercent) || 90;
  const warnTemp = Number(sc.warnTemp) || 80;
  const items = [];

  if (!d || d.enabled === false) {
    items.push(el("span", { class: "sys-label" }, "Loading system stats…"));
  } else if (d.error) {
    items.push(el("span", { class: "stat-error" }, "⚠️ " + d.error));
  } else {
    if (sysShown("cpu") && d.cpu != null) {
      items.push(sysItem("CPU", `${Math.round(d.cpu)}%`, { pct: d.cpu, warn: d.cpu >= warnPct, title: `${d.cores} cores` }));
    }
    if (sysShown("temp") && d.temp) {
      items.push(sysItem("CPU temp", formatTemp(d.temp.c), { warn: d.temp.c >= warnTemp, title: `Sensor: ${d.temp.sensor}` }));
    }
    if (sysShown("ram") && d.memory) {
      const used = d.memory.total - d.memory.available;
      const pct = used / d.memory.total * 100;
      items.push(sysItem("RAM free", formatBytes(d.memory.available), {
        pct, warn: pct >= warnPct, title: `${formatBytes(used)} used of ${formatBytes(d.memory.total)} (${Math.round(pct)}%)` }));
    }
    if (sysShown("disk")) {
      for (const k of d.disks) {
        if (k.error) {
          items.push(sysItem(`${k.label} free`, "n/a", { warn: true, title: `${k.path}: ${k.error}` }));
          continue;
        }
        const pct = k.total ? k.used / k.total * 100 : 0;
        items.push(sysItem(`${k.label} free`, formatBytes(k.free), {
          pct, warn: pct >= warnPct, title: `${k.path}: ${formatBytes(k.used)} used of ${formatBytes(k.total)} (${Math.round(pct)}%)` }));
      }
    }
    if (sysShown("load") && d.load) {
      items.push(sysItem("Load", d.load[0].toFixed(2), {
        warn: d.cores && d.load[0] > d.cores, title: `1 / 5 / 15 min: ${d.load.join(" / ")} (${d.cores} cores)` }));
    }
    if (sysShown("uptime") && d.uptime != null) items.push(sysItem("Uptime", formatDuration(d.uptime)));
  }

  bar.replaceChildren(
    el("div", { class: "sys-host", title: d?.platform || "" }, "🖥 ", sc.label || d?.hostname || "This server"),
    ...items,
    el("button", { class: "btn small edit-only", type: "button", onclick: editSystemOptions }, "Edit"),
  );
}

async function refreshSystem() {
  if (!cfg.system?.enabled) return renderSystem(null);
  try {
    lastSystem = await api("GET", "/api/system");
  } catch (e) {
    lastSystem = { error: e.message };
  }
  renderSystem(lastSystem);
}

function startSystemPolling() {
  clearInterval(systemTimer);
  refreshSystem();
  if (cfg.system?.enabled) {
    systemTimer = setInterval(refreshSystem, Math.max(2, Number(cfg.system.interval) || 10) * 1000);
  }
}

function editSystemOptions() {
  const sc = cfg.system || {};
  const values = {
    ...sc,
    show: Object.fromEntries(SYSTEM_METRICS.map(([k]) => [k, sc.show?.[k] !== false])),
    disksText: (sc.disks || DEFAULT_DISKS).map((d) => d.label && d.label !== d.path ? `${d.path} = ${d.label}` : d.path).join("\n"),
  };
  openForm("System stats", [
    { key: "label", label: "Name shown", placeholder: lastSystem?.hostname || "Leave blank to use the hostname" },
    ...SYSTEM_METRICS.map(([k, label]) => ({ key: `show.${k}`, label: `Show ${label.toLowerCase()}`, type: "checkbox" })),
    { key: "disksText", label: "Disks", type: "textarea", short: true, placeholder: "/ = System\n/mnt/media = Media",
      hint: "One per line: path = label. In Docker, mount host drives into the container and list the mounted paths." },
    { key: "tempUnit", label: "Temperature unit", type: "select", default: "C", options: [["C", "Celsius (°C)"], ["F", "Fahrenheit (°F)"]] },
    { key: "warnTemp", label: "Warn above temperature (°C)", type: "number", default: 80 },
    { key: "warnPercent", label: "Warn above usage (%)", type: "number", default: 90, hint: "Applies to CPU, RAM and disk." },
    { key: "interval", label: "Refresh every (seconds)", type: "number", default: 10 },
  ], values, {
    onSave: (v) => {
      const disks = v.disksText.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
        const [path, ...rest] = line.split("=");
        return { path: path.trim(), label: rest.join("=").trim() || path.trim() };
      });
      if (!disks.length && v.show.disk) throw new Error("Add at least one disk path, or untick “Show free disk space”");
      delete v.disksText;
      if (!v.label) delete v.label;
      cfg.system = { ...sc, ...v, disks };
      save();
      startSystemPolling();
    },
  });
}

// ---------- weather ----------

// WMO weather codes → [label, day icon, night icon]
const WMO = {
  0: ["Clear", "☀️", "🌙"], 1: ["Mainly clear", "🌤️", "🌙"], 2: ["Partly cloudy", "⛅", "☁️"], 3: ["Overcast", "☁️"],
  45: ["Fog", "🌫️"], 48: ["Freezing fog", "🌫️"],
  51: ["Light drizzle", "🌦️"], 53: ["Drizzle", "🌦️"], 55: ["Heavy drizzle", "🌧️"],
  56: ["Freezing drizzle", "🌧️"], 57: ["Freezing drizzle", "🌧️"],
  61: ["Light rain", "🌦️"], 63: ["Rain", "🌧️"], 65: ["Heavy rain", "🌧️"],
  66: ["Freezing rain", "🌧️"], 67: ["Freezing rain", "🌧️"],
  71: ["Light snow", "🌨️"], 73: ["Snow", "🌨️"], 75: ["Heavy snow", "❄️"], 77: ["Snow grains", "🌨️"],
  80: ["Light showers", "🌦️"], 81: ["Showers", "🌧️"], 82: ["Heavy showers", "⛈️"],
  85: ["Snow showers", "🌨️"], 86: ["Heavy snow showers", "❄️"],
  95: ["Thunderstorm", "⛈️"], 96: ["Thunderstorm, hail", "⛈️"], 99: ["Thunderstorm, hail", "⛈️"],
};

function wmo(code, isDay = true) {
  const [label, day, night] = WMO[code] || ["Unknown", "🌡️"];
  return { label, icon: !isDay && night ? night : day };
}

async function refreshWeather() {
  const box = $("#weather");
  if (!cfg.weather?.enabled) return box.classList.add("hidden");
  let w;
  try {
    w = await api("GET", "/api/weather");
  } catch (e) {
    box.replaceChildren(el("span", { class: "weather-error", title: e.message }, "🌡️ Weather unavailable"));
    return;
  }
  if (!w.enabled) return box.classList.add("hidden");

  const deg = (v) => `${Math.round(v)}°`;
  const c = w.current;
  const now = wmo(c.code, c.isDay);
  const [today, ...rest] = w.daily;

  box.replaceChildren(
    el("div", {
      class: "weather-now",
      title: `Feels like ${deg(c.feelsLike)} · Humidity ${c.humidity}% · Wind ${Math.round(c.wind)} ${w.units.wind}`,
    },
      el("span", { class: "weather-icon", "aria-hidden": "true" }, now.icon),
      el("div", {},
        el("div", { class: "weather-temp" }, deg(c.temp) + w.units.temp.slice(1)),
        el("div", { class: "weather-meta" },
          el("strong", {}, now.label),
          el("br"),
          [w.location, today && `H ${deg(today.max)} L ${deg(today.min)}`].filter(Boolean).join(" · "),
          el("br"),
          // Open-Meteo's data is CC BY 4.0, which requires this credit.
          el("a", { class: "weather-credit", href: "https://open-meteo.com/", target: "_blank", rel: "noopener noreferrer",
            title: "Weather data by Open-Meteo.com (CC BY 4.0)" }, "Weather by Open-Meteo")),
      ),
    ),
    el("div", { class: "forecast" },
      rest.slice(0, 3).map((d) => {
        const f = wmo(d.code);
        const day = new Date(d.date + "T12:00").toLocaleDateString([], { weekday: "short" });
        return el("div", { class: "forecast-day", title: f.label },
          el("span", {}, day),
          el("span", { class: "fi", "aria-hidden": "true" }, f.icon),
          el("span", { class: "hi" }, deg(d.max)),
          el("span", {}, deg(d.min)));
      }),
    ),
  );
  box.classList.remove("hidden");
}

/** Turn "City", "City, Region/Country" or "lat, lon" into coordinates. */
async function resolveLocation(text) {
  const m = text.match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (m) return { name: "", label: text.trim(), latitude: Number(m[1]), longitude: Number(m[2]) };
  return api("GET", "/api/geocode?q=" + encodeURIComponent(text));
}

// ---------- RSS ----------

function timeAgo(ms) {
  const s = Math.round((Date.now() - ms) / 1000);
  if (Number.isNaN(s)) return "";
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return d < 30 ? `${d}d ago` : new Date(ms).toLocaleDateString();
}

/**
 * mode "auto":    fetch only if our copy is older than RSS_REFRESH_MS
 * mode "reload":  fetch now (the server may still answer from its cache)
 * mode "refresh": ask the server to re-download the feeds too
 */
async function loadRss(mode = "auto") {
  if (!rssOn() || rssLoading) return;
  if (mode === "auto" && rssData && Date.now() - rssFetchedAt < RSS_REFRESH_MS) return;
  rssLoading = true;
  if (isRssTab(activeTab)) render();
  try {
    rssData = (await api("GET", "/api/rss" + (mode === "refresh" ? "?refresh=1" : ""))).feeds;
    rssFetchedAt = Date.now();
  } catch (e) {
    toast("Couldn't load feeds: " + e.message, true);
  } finally {
    rssLoading = false;
    if (isRssTab(activeTab) && !$("#searchInput").value.trim()) render();
  }
}

function renderRss(root) {
  const feeds = cfg.rss.feeds;
  const perFeed = Math.max(1, Number(cfg.rss.itemsPerFeed) || 8);

  root.append(el("div", { class: "rss-bar" },
    el("span", { class: "rss-updated" }, rssLoading ? "Updating…" : rssFetchedAt ? `Updated ${timeAgo(rssFetchedAt)}` : ""),
    el("span", { class: "spacer" }),
    el("button", { class: "btn small edit-only", type: "button", onclick: () => editFeed() }, "+ Add feed"),
    el("button", { class: "btn small", type: "button", disabled: rssLoading, title: "Re-download all feeds", onclick: () => loadRss("refresh") }, "↻ Refresh"),
  ));
  if (!feeds.length) {
    root.append(el("p", { class: "empty" }, "No feeds yet — click ✏️ then “+ Add feed”."));
    return;
  }

  const grid = el("div", { class: "feed-grid" });
  feeds.forEach((feed, fi) => {
    const data = rssData?.[feed.url];
    let body;
    if (!data) body = el("p", { class: "feed-note" }, rssLoading ? "Loading…" : "Not loaded yet");
    else if (data.error) body = el("p", { class: "feed-note error" }, `Couldn't load this feed: ${data.error}`);
    else if (!data.items.length) body = el("p", { class: "feed-note" }, "No items");
    else {
      body = el("ul", { class: "feed-items" }, data.items.slice(0, perFeed).map((item) => el("li", {},
        el("a", { class: "feed-title", href: safeUrl(item.link), target: "_blank", rel: "noopener noreferrer" }, item.title || "(untitled)"),
        cfg.rss.showSummaries !== false && item.summary ? el("p", { class: "feed-summary" }, item.summary) : null,
        item.date ? el("time", { datetime: item.date, title: new Date(item.date).toLocaleString() }, timeAgo(Date.parse(item.date))) : null,
      )));
    }

    const title = feed.name || data?.title || feed.url;
    const site = data?.link && safeUrl(data.link) !== "#" ? data.link : null;
    grid.append(el("article", { class: "feed-card" },
      el("header", { class: "feed-head" },
        el("h3", {}, site ? el("a", { href: site, target: "_blank", rel: "noopener noreferrer" }, title) : title),
        el("span", { class: "edit-only", style: "gap:4px" },
          el("button", { class: "btn small", type: "button", onclick: () => editFeed(fi) }, "Edit"),
          el("button", { class: "btn small", type: "button", title: "Move left", disabled: fi === 0, onclick: () => moveFeed(fi, -1) }, "←"),
          el("button", { class: "btn small", type: "button", title: "Move right", disabled: fi === feeds.length - 1, onclick: () => moveFeed(fi, 1) }, "→"),
        ),
      ),
      body,
    ));
  });
  root.append(grid);
}

function editFeed(fi) {
  const feeds = cfg.rss.feeds;
  const isNew = fi == null;
  openForm(isNew ? "Add feed" : "Edit feed", [
    { key: "url", label: "Feed URL", type: "url", required: true, placeholder: "https://example.com/feed.xml",
      hint: "RSS or Atom. GitHub releases work too: https://github.com/<owner>/<repo>/releases.atom" },
    { key: "name", label: "Name (optional)", hint: "Leave blank to use the feed's own title." },
  ], isNew ? {} : feeds[fi], {
    onSave: (v) => {
      const dupe = feeds.findIndex((f) => f.url === v.url);
      if (dupe !== -1 && dupe !== fi) throw new Error("That feed is already on the page");
      const feed = { name: v.name, url: v.url };
      if (!feed.name) delete feed.name;
      if (isNew) feeds.push(feed); else feeds[fi] = feed;
      save();
    },
    onDelete: isNew ? null : () => { feeds.splice(fi, 1); save(); },
  });
}

function moveFeed(fi, dir) {
  const feeds = cfg.rss.feeds;
  const [f] = feeds.splice(fi, 1);
  feeds.splice(fi + dir, 0, f);
  save();
}

function editRssOptions() {
  openForm("News tab", [
    { key: "tabName", label: "Tab name", default: "News", required: true },
    { key: "icon", label: "Icon (optional)", default: "📰", hint: "An emoji shown before the name." },
    { key: "itemsPerFeed", label: "Items per feed", type: "number", default: 8 },
    { key: "showSummaries", label: "Show item summaries", type: "checkbox" },
  ], cfg.rss, {
    onSave: (v) => {
      const clash = cfg.tabs.findIndex((t) => slug(t.name) === slug(v.tabName));
      if (clash !== -1) throw new Error(`A tab called “${cfg.tabs[clash].name}” already exists`);
      cfg.rss = { ...cfg.rss, ...v, itemsPerFeed: Math.min(30, Math.max(1, v.itemsPerFeed || 8)) };
      save();
    },
  });
}

// ---------- uptime history ----------

let lastHistory = null;

async function refreshHistory() {
  if (cfg.showUptime === false) return applyHistory(null);
  try {
    lastHistory = await api("GET", "/api/history");
    applyHistory(lastHistory);
  } catch { /* keep the last bars */ }
}

/** A thin 24-hour bar along the bottom of each checked tile (one segment per 30 minutes). */
function applyHistory(h) {
  document.querySelectorAll(".tile[data-status-url]").forEach((tile) => {
    const s = h?.services?.[tile.dataset.statusUrl];
    let bar = tile.querySelector(".uptime-bar");
    if (!s || cfg.showUptime === false) {
      bar?.remove();
      tile.classList.remove("has-uptime");
      return;
    }
    if (!bar) {
      bar = el("div", { class: "uptime-bar" });
      tile.append(bar);
      tile.classList.add("has-uptime");
    }
    bar.title = s.uptime == null ? "No checks yet" : `Last 24 hours: ${s.uptime}% up`;
    bar.replaceChildren(...s.buckets.map((p) =>
      el("span", { class: p == null ? "none" : p >= 99.9 ? "up" : p > 0 ? "partial" : "down" })));
  });
}

// ---------- upcoming (calendar) ----------

const UPCOMING_REFRESH_MS = 10 * 60 * 1000;
const KIND_ICONS = { episode: "📺", movie: "🎬", album: "🎵", book: "📚", magazine: "📖" };
const KIND_LABELS = [["episode", "TV episodes (Sonarr)"], ["movie", "Movies (Radarr)"], ["album", "Music (Lidarr)"],
  ["book", "Books (Readarr)"], ["magazine", "Magazines (Magarr)"]];
let upcomingData = null;
let upcomingFetchedAt = 0;
let upcomingLoading = false;

async function loadUpcoming(mode = "auto") {
  if (!upcomingOn() || upcomingLoading) return;
  if (mode === "auto" && upcomingData && Date.now() - upcomingFetchedAt < UPCOMING_REFRESH_MS) return;
  upcomingLoading = true;
  if (isUpcomingTab(activeTab)) render();
  try {
    upcomingData = await api("GET", "/api/upcoming" + (mode === "refresh" ? "?refresh=1" : ""));
    upcomingFetchedAt = Date.now();
  } catch (e) {
    toast("Couldn't load the calendar: " + e.message, true);
  } finally {
    upcomingLoading = false;
    if (isUpcomingTab(activeTab) && !$("#searchInput").value.trim()) render();
  }
}

const pad2 = (n) => String(n).padStart(2, "0");
const dayKey = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;

/** Local calendar day of an item; all-day dates keep their own date (no timezone shift). */
const itemDay = (it) => (it.allDay ? String(it.date).slice(0, 10) : dayKey(new Date(it.date)));

function dayLabel(key, todayKey) {
  const diff = Math.round((Date.parse(key) - Date.parse(todayKey)) / 864e5);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  return new Date(key + "T12:00").toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
}

function renderUpcoming(root) {
  root.append(el("div", { class: "rss-bar" },
    el("span", { class: "rss-updated" }, upcomingLoading ? "Updating…" : upcomingFetchedAt ? `Updated ${timeAgo(upcomingFetchedAt)}` : ""),
    el("span", { class: "spacer" }),
    el("button", { class: "btn small", type: "button", disabled: upcomingLoading, title: "Fetch the calendars again", onclick: () => loadUpcoming("refresh") }, "↻ Refresh"),
  ));
  const d = upcomingData;
  if (!d) {
    root.append(el("p", { class: "empty" }, upcomingLoading ? "Loading…" : "Not loaded yet"));
    return;
  }
  for (const src of d.sources.filter((x) => x.error)) {
    root.append(el("p", { class: "feed-note error" }, `⚠️ ${src.name}: ${src.error}`));
  }
  if (!d.sources.length) {
    root.append(el("p", { class: "empty" }, "Add a Sonarr, Radarr, Lidarr, Readarr or Magarr widget to a service and its calendar will appear here."));
    return;
  }
  if (!d.items.length) {
    root.append(el("p", { class: "empty" }, "Nothing scheduled in this period."));
    return;
  }

  const todayKey = dayKey(new Date());
  const days = new Map();
  for (const it of d.items) {
    const k = itemDay(it);
    if (!days.has(k)) days.set(k, []);
    days.get(k).push(it);
  }
  const wrap = el("div", { class: "upcoming" });
  for (const [k, items] of [...days].sort(([a], [b]) => a.localeCompare(b))) {
    const past = k < todayKey;
    wrap.append(el("section", { class: "up-day" + (past ? " past" : "") },
      el("h2", {}, dayLabel(k, todayKey), el("span", { class: "group-tab" }, ` · ${items.length}`)),
      el("div", { class: "up-list" }, items.map((it) => {
        const released = past || (k === todayKey && !it.allDay && Date.parse(it.date) < Date.now());
        const badge = it.hasFile ? el("span", { class: "up-badge ok" }, "Downloaded")
          : released ? el("span", { class: "up-badge missing" }, "Missing") : null;
        return el("a", { class: "up-item", href: safeUrl(it.link), target: "_blank", rel: "noopener noreferrer" },
          el("span", { class: "up-icon", "aria-hidden": "true" }, KIND_ICONS[it.kind] || "•"),
          el("span", { class: "up-time" }, it.allDay ? "" : new Date(it.date).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
          el("span", { class: "up-main" },
            el("span", { class: "up-title" }, it.title),
            el("span", { class: "up-detail" }, [it.detail, it.source].filter(Boolean).join(" · "))),
          badge);
      })),
    ));
  }
  root.append(wrap);
}

function editUpcomingOptions() {
  const uc = cfg.upcoming || {};
  const values = { ...uc, kinds: Object.fromEntries(KIND_LABELS.map(([k]) => [k, uc.kinds?.[k] !== false])) };
  openForm("Upcoming tab", [
    { key: "tabName", label: "Tab name", default: "Upcoming", required: true },
    { key: "icon", label: "Icon (optional)", default: "📅", hint: "An emoji shown before the name." },
    { key: "days", label: "Days ahead", type: "number", default: 7 },
    { key: "pastDays", label: "Days back", type: "number", default: 1, hint: "Recent days stay visible, marked Downloaded or Missing." },
    ...KIND_LABELS.map(([k, label]) => ({ key: `kinds.${k}`, label: `Show ${label}`, type: "checkbox" })),
  ], values, {
    onSave: (v) => {
      const clash = cfg.tabs.findIndex((t) => slug(t.name) === slug(v.tabName));
      if (clash !== -1) throw new Error(`A tab called “${cfg.tabs[clash].name}” already exists`);
      cfg.upcoming = {
        ...uc, ...v,
        days: Math.min(60, Math.max(1, v.days || 7)),
        pastDays: Math.min(30, Math.max(0, Number.isFinite(v.pastDays) ? v.pastDays : 1)),
      };
      save();
    },
  });
}

// ---------- find services ----------

function findServices() {
  const ti = specialAt(activeTab) ? 0 : activeTab; // found services go to the tab you're on
  let proxmox = 0;
  cfg.tabs.forEach((t) => t.groups.forEach((g) => g.services.forEach((s) => {
    if (s.widget?.type === "proxmox") proxmox++;
  })));

  const fields = [
    { key: "useConfigHosts", label: "Scan the machines your services already run on", type: "checkbox" },
  ];
  if (proxmox) fields.push({ key: "useProxmox", label: `Include running Proxmox VMs and containers (via ${proxmox === 1 ? "your Proxmox widget" : `${proxmox} Proxmox widgets`})`, type: "checkbox" });
  fields.push({ key: "hosts", label: "Other hosts or a subnet (optional)", placeholder: "nas.lan, 192.168.1.10, 192.168.1.0/24",
    hint: "Separate with commas or spaces. A /24 subnet scans 254 addresses and takes up to a minute. Only each app's front page is read." });

  openForm("Find services", fields, { useConfigHosts: true, useProxmox: true }, {
    onSave: async (v) => {
      if (!v.useConfigHosts && !v.useProxmox && !v.hosts) throw new Error("Choose at least one place to look");
      ensureToken();
      toast("Scanning… this can take up to a minute");
      const r = await api("POST", "/api/discover", { useProxmox: false, ...v }); // throws → dialog stays open
      setTimeout(() => showFoundServices(r, ti), 0); // after this dialog closes
    },
    submitLabel: "Scan",
  });
}

function showFoundServices(r, ti) {
  const notes = r.notes?.length ? ` Note: ${r.notes.join("; ")}.` : "";
  if (!r.candidates.length) {
    toast(`No new services found on ${r.hosts} host${r.hosts === 1 ? "" : "s"} (${r.openPorts} open ports checked).${notes}`, !!notes);
    return;
  }
  const groups = cfg.tabs[ti].groups;
  const fields = r.candidates.map((c, i) => ({
    key: `pick.${i}`,
    type: "checkbox",
    default: !!c.app && !c.already, // recognised, new apps start ticked
    label: `${c.name} · ${c.url}${c.source ? ` (${c.source})` : ""}` +
      (c.already ? ` · probably already on your dashboard as “${c.already}”` : c.app ? "" : " · not recognised"),
  }));
  fields.push(
    { key: "group", label: `Add the ticked services to this group on the “${cfg.tabs[ti].name}” tab`, type: "select",
      default: groups.length ? "0" : "__new__",
      options: [...groups.map((g, i) => [String(i), g.name]), ["__new__", "A new group called “Discovered”"]],
      hint: `Scanned ${r.hosts} hosts in ${r.seconds} s.${notes}` },
    { key: "widgets", label: "Set up widgets for the apps that have one (add each API key afterwards)", type: "checkbox" },
  );

  openForm(`Found ${r.candidates.length} new service${r.candidates.length === 1 ? "" : "s"}`, fields, { widgets: true }, {
    onSave: (v) => {
      const picked = r.candidates.filter((_, i) => v.pick?.[i]);
      if (!picked.length) throw new Error("Tick at least one service, or press Cancel");
      let group = groups[Number(v.group)];
      if (v.group === "__new__" || !group) {
        group = { name: "Discovered", services: [] };
        groups.push(group);
      }
      let withWidget = 0;
      for (const c of picked) {
        const svc = { name: c.name, url: c.url };
        if (c.icon) svc.icon = c.icon;
        if (v.widgets && c.widget && widgetPresets[c.widget]) {
          group.services.push({ id: newId(), ...svc, widget: { type: c.widget } });
          withWidget++;
        } else {
          group.services.push(svc);
        }
      }
      activeTab = ti;
      save();
      toast(`Added ${picked.length} service${picked.length === 1 ? "" : "s"}` +
        (withWidget ? `. Edit ${withWidget === 1 ? "its tile" : "their tiles"} to add the API key${withWidget === 1 ? "" : "s"}.` : "."));
    },
    submitLabel: "Add services",
  });
}

// ---------- alerts ----------

async function editAlerts() {
  let st = {};
  try { st = await api("GET", "/api/alerts/status"); } catch { /* show the form anyway */ }
  const ac = cfg.alerts || {};
  const statusLine = st.lastError ? `⚠️ Last delivery failed: ${st.lastError}`
    : st.lastSent ? `Last alert sent ${timeAgo(st.lastSent * 1000)}: ${st.lastEvents?.at(-1) || ""}`
    : "No alerts sent yet.";

  const testButton = (input) => {
    const result = el("span", { class: "hint" }, statusLine);
    return el("div", { class: "widget-test" },
      el("button", { type: "button", class: "btn small", onclick: async () => {
        result.textContent = "Sending…";
        try {
          ensureToken();
          await api("POST", "/api/alerts/test", {
            webhookUrl: input.value.trim(),
            type: $('#modalBody [data-key="type"]').value,
          });
          result.textContent = "✓ Test sent: check your channel";
        } catch (err) {
          result.textContent = "⚠️ " + err.message;
        }
      } }, "Send test"),
      result);
  };

  openForm("Alerts", [
    { key: "enabled", label: "Send alerts", type: "checkbox", default: false },
    { key: "type", label: "Send to", type: "select", default: "discord",
      options: [["discord", "Discord webhook"], ["webhook", "Other webhook (JSON POST, e.g. Home Assistant, n8n)"]] },
    { key: "webhookUrl", label: "Webhook URL", type: "password", extra: testButton,
      placeholder: st.webhookSet ? "•••••••• saved (leave blank to keep)" : "https://discord.com/api/webhooks/…",
      hint: "Discord: Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Kept on the server only." },
    { key: "services", label: "When a service goes down, and when it's back", type: "checkbox", default: true },
    { key: "widgets", label: "When an app widget shows a warning (the values in red)", type: "checkbox", default: true },
    { key: "system", label: "When this server's CPU temperature, RAM or disk passes the system stats thresholds", type: "checkbox", default: true },
    { key: "failures", label: "Alert after this many checks in a row", type: "number", default: 2,
      hint: `Checks run every ${cfg.statusInterval || 30} seconds (Settings → Status check interval).` },
  ], { ...ac }, {
    onSave: async (v) => {
      const url = v.webhookUrl;
      delete v.webhookUrl;
      if (url && !/^https?:\/\//i.test(url)) throw new Error("The webhook URL should start with https://");
      if (v.enabled && !url && !st.webhookSet) throw new Error("Enter a webhook URL to send alerts");
      if (url) {
        ensureToken();
        await api("PUT", "/api/secrets/_alerts", { webhookUrl: url }); // throws → dialog stays open
      }
      cfg.alerts = { ...ac, ...v, failures: Math.max(1, v.failures || 2) };
      save();
    },
  });
}

// ---------- clock & search ----------

function tickClock() {
  const now = new Date();
  $("#clockTime").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("#clockDate").textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  const h = now.getHours();
  $("#greeting").textContent = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function applyFilter() {
  const q = $("#searchInput").value.trim().toLowerCase();
  document.querySelectorAll(".group").forEach((group) => {
    let visible = 0;
    group.querySelectorAll(".tile[data-search]").forEach((tile) => {
      const match = !q || tile.dataset.search.includes(q);
      tile.classList.toggle("hidden", !match);
      if (match) visible++;
    });
    group.classList.toggle("hidden", !!q && visible === 0);
  });
}

function onSearchSubmit(e) {
  e.preventDefault();
  const q = $("#searchInput").value.trim();
  if (!q) return;
  const matches = [...document.querySelectorAll(".tile[data-search]:not(.hidden)")];
  if (matches.length === 1) return window.open(matches[0].href, "_blank", "noopener");
  if (/^https?:\/\//i.test(q)) return window.open(q, "_blank", "noopener");
  const engine = cfg.search?.engine || "https://duckduckgo.com/?q=";
  window.open(engine + encodeURIComponent(q), "_blank", "noopener");
}

// ---------- editing ----------

function openForm(title, fields, values, { onSave, onDelete, deleteMessage = "Delete this?", submitLabel = "Save" } = {}) {
  const dlg = $("#modal");
  $("#modalTitle").textContent = title;
  $("#modalForm [type=submit]").textContent = submitLabel;
  const body = $("#modalBody");
  body.replaceChildren();

  for (const f of fields) {
    const id = "f_" + f.key.replace(/\W/g, "_");
    const val = getPath(values, f.key);
    let input;
    if (f.type === "select") {
      input = el("select", { id });
      const groups = new Map();
      for (const [v, label, group] of f.options) {
        const opt = el("option", { value: v, selected: v === (val ?? f.default) }, label);
        if (!group) { input.append(opt); continue; }
        if (!groups.has(group)) input.append(groups.set(group, el("optgroup", { label: group })).get(group));
        groups.get(group).append(opt);
      }
    } else if (f.type === "textarea") {
      input = el("textarea", { id, spellcheck: "false", class: f.short ? "short" : null, placeholder: f.placeholder });
      input.value = val ?? "";
    } else if (f.type === "checkbox") {
      input = el("input", { id, type: "checkbox", checked: val ?? f.default ?? true });
    } else {
      input = el("input", { id, type: f.type || "text", placeholder: f.placeholder, required: f.required });
      input.value = val ?? f.default ?? "";
    }
    input.dataset.key = f.key;
    body.append(f.type === "checkbox"
      ? el("div", { class: "field checkbox" }, input, el("label", { for: id }, f.label))
      : el("div", { class: "field" }, el("label", { for: id }, f.label), input, f.extra?.(input, values), f.hint ? el("span", { class: "hint" }, f.hint) : null));
  }
  dlg.onclose = () => applyTheme(); // drop any live preview if the dialog was cancelled

  const delBtn = $("#modalDelete");
  delBtn.hidden = !onDelete;
  delBtn.onclick = () => { if (confirm(deleteMessage)) { dlg.close(); onDelete(); } };
  $("#modalCancel").onclick = () => dlg.close();
  $("#modalForm").onsubmit = async (e) => {
    e.preventDefault();
    const out = {};
    body.querySelectorAll("[data-key]").forEach((inp) => {
      const v = inp.type === "checkbox" ? inp.checked : inp.type === "number" ? Number(inp.value) : inp.value.trim();
      setPath(out, inp.dataset.key, v);
    });
    const submit = e.target.querySelector("[type=submit]");
    submit.disabled = true;
    try {
      await onSave(out);
      dlg.close();
    } catch (err) {
      toast(err.message, true); // dialog stays open so the input can be fixed
    } finally {
      submit.disabled = false;
    }
  };
  dlg.showModal();
  body.querySelector("input, textarea, select")?.focus();
}

function getPath(obj, path) { return path.split(".").reduce((o, k) => o?.[k], obj); }
function setPath(obj, path, v) {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((o, k) => (o[k] ??= {}), obj)[last] = v;
}

const SERVICE_FIELDS = [
  { key: "name", label: "Name", required: true },
  { key: "url", label: "URL", type: "url", required: true, placeholder: "http://192.168.1.10:8080" },
  { key: "description", label: "Description" },
  { key: "icon", label: "Icon", placeholder: "di:plex  ·  🚀  ·  https://…/icon.png",
    hint: "“di:<name>” uses dashboard-icons (e.g. di:proxmox), or an emoji, or an image URL." },
  { key: "statusUrl", label: "Status-check URL (optional)", type: "url", hint: "Checked instead of the main URL, e.g. an internal address or /health endpoint." },
  { key: "check", label: "Show online/offline status", type: "checkbox", default: true },
];

function serviceFields() {
  const presets = Object.values(widgetPresets);
  if (!presets.length) return SERVICE_FIELDS;
  return [...SERVICE_FIELDS, {
    key: "widget.type", label: "Widget: live stats from the app's API", type: "select", extra: widgetOptions,
    options: [["", "None"], ...presets.map((p) => [p.id, p.name, p.group])],
  }];
}

const newId = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

/** Preset-specific inputs under the widget select. Secrets use data-key "secrets.*". */
function widgetOptions(select, values) {
  const box = el("div", { class: "widget-opts" });
  const current = values.widget || {};
  let savedSecrets = [];

  const draw = () => {
    box.replaceChildren();
    const preset = widgetPresets[select.value];
    if (!preset) return;
    const same = select.value === current.type;
    if (preset.help) box.append(el("p", { class: "hint widget-help" }, "🔑 " + preset.help));

    for (const o of preset.options) {
      const id = "w_" + o.key;
      const secret = o.type === "secret";
      const saved = secret && same && savedSecrets.includes(o.key);
      const val = same && !secret ? current.options?.[o.key] ?? "" : "";
      let input;
      if (o.type === "select") {
        input = el("select", { id }, o.options.map(([v, label]) => el("option", { value: v, selected: v === (val || o.options[0][0]) }, label)));
      } else if (o.type === "textarea") {
        input = el("textarea", { id, class: "short", spellcheck: "false", placeholder: o.placeholder, required: o.required });
        input.value = val;
      } else {
        input = el("input", {
          id,
          type: secret ? "password" : "text",
          autocomplete: secret ? "new-password" : "off",
          placeholder: saved ? "•••••••• saved (leave blank to keep)" : o.placeholder,
          required: o.required && !saved,
        });
        input.value = val;
      }
      input.dataset.key = (secret ? "secrets." : "widget.options.") + o.key;
      let remove = null;
      if (saved) {
        // a saved secret is never sent to the browser, so blank means "keep": offer an explicit remove
        remove = el("button", { type: "button", class: "btn small", onclick: () => {
          input.value = "";
          input.dataset.cleared = "1";
          input.required = false;
          input.placeholder = "Will be removed when you save";
          remove.disabled = true;
        } }, "Remove saved key");
        input.addEventListener("input", () => {
          if (!input.value) return;
          delete input.dataset.cleared; // typing a new value replaces it instead
          remove.disabled = false;
        });
      }
      box.append(el("div", { class: "field" }, el("label", { for: id }, o.label), input, remove, o.hint ? el("span", { class: "hint" }, o.hint) : null));
    }

    const url = el("input", { id: "w_url", type: "text", placeholder: "Leave blank to use the service URL's host and port" });
    url.value = same ? current.url || "" : "";
    url.dataset.key = "widget.url";
    box.append(el("div", { class: "field" },
      el("label", { for: "w_url" }, "API base URL (optional)"), url,
      el("span", { class: "hint" }, "Only needed if the app is behind a reverse-proxy sub-path (e.g. https://proxy.lan/sonarr) or its API is on a different address.")));

    const result = el("div", { class: "tile-stats widget-test-result" });
    box.append(el("div", { class: "widget-test" },
      el("button", { type: "button", class: "btn small", onclick: () => testWidget(box, select, values, result) }, "Test connection"),
      el("button", {
        type: "button", class: "btn small danger", disabled: !savedSecrets.length,
        title: savedSecrets.length ? "Remove the saved API key from the server" : "No API key saved",
        onclick: () => deleteSecrets(values.id, savedSecrets).then((ok) => { if (ok) { savedSecrets = []; draw(); } }),
      }, "Delete API key"),
      result));
  };

  select.addEventListener("change", draw);
  draw();
  if (current.type && values.id) {
    api("GET", `/api/secrets/${encodeURIComponent(values.id)}`)
      .then((r) => { savedSecrets = r.set; draw(); })
      .catch(() => {});
  }
  return box;
}

/** Remove a service's saved secrets now, without saving the rest of the dialog. */
async function deleteSecrets(id, keys) {
  if (!confirm("Delete the saved API key for this service? The widget stops working until you add a new one.")) return false;
  try {
    ensureToken();
    await api("PUT", `/api/secrets/${encodeURIComponent(id)}`, Object.fromEntries(keys.map((k) => [k, null])));
    toast("API key deleted");
    refreshWidgets();
    return true;
  } catch (err) {
    toast("Couldn't delete the API key: " + err.message, true);
    return false;
  }
}

async function testWidget(box, select, values, result) {
  const out = {};
  box.querySelectorAll("[data-key]").forEach((inp) => setPath(out, inp.dataset.key, inp.value.trim()));
  const url = $('#modalBody [data-key="url"]').value.trim();
  result.replaceChildren(el("span", { class: "stat-label" }, "Testing…"));
  try {
    ensureToken();
    const r = await api("POST", "/api/widgets/test", {
      widget: { type: select.value, ...out.widget },
      url,
      id: values.id,
      secrets: out.secrets,
    });
    renderStats(result, r);
  } catch (err) {
    renderStats(result, { error: err.message });
  }
}

async function loadWidgetPresets() {
  try {
    const { presets } = await api("GET", "/api/widgets/presets");
    widgetPresets = Object.fromEntries(presets.map((p) => [p.id, p]));
  } catch (e) {
    toast("Couldn't load widget types: " + e.message, true);
  }
}

async function editService(ti, gi, si) {
  if (!Object.keys(widgetPresets).length) await loadWidgetPresets(); // retry if the first load failed
  const group = cfg.tabs[ti].groups[gi];
  const isNew = si == null;
  const old = isNew ? {} : group.services[si];
  openForm(isNew ? `Add service to ${group.name}` : "Edit service", serviceFields(), old, {
    onSave: async (v) => {
      const secrets = v.secrets || {};
      const w = v.widget || {};
      delete v.secrets;
      delete v.widget;
      for (const k of ["description", "icon", "statusUrl"]) if (!v[k]) delete v[k];
      if (v.check) delete v.check; // default is true

      if (w.type) {
        const options = Object.fromEntries(Object.entries(w.options || {}).filter(([, x]) => x !== ""));
        // keep hand-edited extras (e.g. "interval") when the type is unchanged
        v.widget = { ...(old.widget?.type === w.type ? old.widget : {}), type: w.type, options };
        if (w.url) v.widget.url = w.url; else delete v.widget.url;
        if (!Object.keys(options).length) delete v.widget.options;
      }
      const id = old.id || (v.widget ? newId() : null);
      const svc = id ? { id, ...v } : v;

      const toStore = Object.fromEntries(Object.entries(secrets).filter(([, x]) => x));
      // null tells the server to delete a saved secret (see update_secrets in server.py)
      document.querySelectorAll('#modalBody [data-cleared="1"]').forEach((inp) => {
        toStore[inp.dataset.key.slice("secrets.".length)] = null;
      });
      if (svc.widget && Object.keys(toStore).length) {
        ensureToken();
        await api("PUT", `/api/secrets/${encodeURIComponent(id)}`, toStore); // throws → dialog stays open
      }
      if (isNew) group.services.push(svc); else group.services[si] = svc;
      save();
    },
    onDelete: isNew ? null : () => { group.services.splice(si, 1); save(); },
  });
}

function editGroup(ti = specialAt(activeTab) ? 0 : activeTab, gi) {
  const isNew = gi == null;
  const fields = [{ key: "name", label: "Group name", required: true }];
  if (cfg.tabs.length > 1) {
    fields.push({ key: "tab", label: "Tab", type: "select", options: cfg.tabs.map((t, i) => [String(i), t.name]) });
  }
  const groups = cfg.tabs[ti].groups;
  openForm(isNew ? "Add group" : "Edit group", fields, { name: isNew ? "" : groups[gi].name, tab: String(ti) }, {
    onSave: (v) => {
      const target = v.tab == null ? ti : Number(v.tab);
      const group = isNew ? { name: v.name, services: [] } : groups.splice(gi, 1)[0];
      group.name = v.name;
      if (!isNew && target === ti) groups.splice(gi, 0, group); // keep position
      else cfg.tabs[target].groups.push(group);
      activeTab = target;
      save();
    },
    onDelete: isNew ? null : () => { groups.splice(gi, 1); save(); },
    deleteMessage: "Delete this group and all of its services?",
  });
}

function moveGroup(ti, gi, dir) {
  const groups = cfg.tabs[ti].groups;
  const [g] = groups.splice(gi, 1);
  groups.splice(gi + dir, 0, g);
  save();
}

function moveService(src, ti, gi, si) {
  if (!src || (src.ti === ti && src.gi === gi && src.si === si)) return;
  const [svc] = cfg.tabs[src.ti].groups[src.gi].services.splice(src.si, 1);
  if (src.ti === ti && src.gi === gi && src.si < si) si--;
  cfg.tabs[ti].groups[gi].services.splice(si, 0, svc);
  dragSrc = null;
  save();
}

function moveServiceToTab(src, ti) {
  if (!src || src.ti === ti) return;
  const groups = cfg.tabs[ti].groups;
  if (!groups.length) groups.push({ name: "General", services: [] });
  const [svc] = cfg.tabs[src.ti].groups[src.gi].services.splice(src.si, 1);
  groups[groups.length - 1].services.push(svc);
  dragSrc = null;
  toast(`Moved ${svc.name} to ${cfg.tabs[ti].name}`);
  save();
}

function editTab(ti) {
  const isNew = ti == null;
  openForm(isNew ? "Add tab" : "Edit tab", [
    { key: "name", label: "Tab name", required: true },
    { key: "icon", label: "Icon (optional)", placeholder: "🎬", hint: "An emoji shown before the name." },
  ], isNew ? {} : cfg.tabs[ti], {
    onSave: (v) => {
      const name = v.name;
      const clash = allTabs().findIndex((t) => slug(t.name) === slug(name));
      if (clash !== -1 && clash !== ti) throw new Error(`A tab called “${allTabs()[clash].name}” already exists`);
      const tab = isNew ? { groups: [] } : cfg.tabs[ti];
      tab.name = name;
      if (v.icon) tab.icon = v.icon; else delete tab.icon;
      if (isNew) {
        cfg.tabs.push(tab);
        activeTab = cfg.tabs.length - 1;
      }
      save();
    },
    onDelete: isNew || cfg.tabs.length < 2 ? null : () => {
      cfg.tabs.splice(ti, 1);
      activeTab = Math.max(0, ti - 1);
      save();
    },
    deleteMessage: "Delete this tab and everything on it?",
  });
}

function moveTab(ti, dir) {
  const [t] = cfg.tabs.splice(ti, 1);
  cfg.tabs.splice(ti + dir, 0, t);
  activeTab = ti + dir;
  save();
}

// ---------- background picker ----------

const formatSize = (n) => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;

/** Upload button + gallery of uploaded images, bound to the background URL input. */
function backgroundPicker(input) {
  const gallery = el("div", { class: "bg-gallery" });
  const fileInput = el("input", { type: "file", accept: "image/jpeg,image/png,image/gif,image/webp,image/avif", hidden: true });

  const select = (url) => {
    input.value = url;
    setBackground(url); // live preview; reverted on cancel
    gallery.querySelectorAll(".bg-thumb").forEach((b) => b.classList.toggle("selected", b.dataset.url === url));
  };

  const refresh = async () => {
    let images = [];
    try { images = (await api("GET", "/api/backgrounds")).images; } catch { /* leave gallery empty */ }
    gallery.replaceChildren(...images.map((img) => el("div", { class: "bg-item" },
      el("button", {
        type: "button",
        class: "bg-thumb" + (img.url === input.value ? " selected" : ""),
        "data-url": img.url,
        title: `${img.name} · ${formatSize(img.size)}`,
        style: `background-image:url("${img.url}")`,
        onclick: () => select(img.url),
      }),
      el("button", {
        type: "button",
        class: "bg-del",
        title: "Delete this image",
        "aria-label": "Delete image",
        onclick: async () => {
          if (!confirm("Delete this image from the server?")) return;
          try {
            ensureToken();
            await api("DELETE", "/api/backgrounds/" + encodeURIComponent(img.name));
            if (input.value === img.url) select("");
            refresh();
          } catch (err) { toast(err.message, true); }
        },
      }, "✕"),
    )));
  };

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    try {
      ensureToken();
      toast("Uploading…");
      const img = await api("POST", "/api/backgrounds", file, {
        "X-Filename": encodeURIComponent(file.name),
        "Content-Type": file.type || "application/octet-stream",
      });
      await refresh();
      select(img.url);
      toast("Uploaded — click Save to keep it as the background");
    } catch (err) { toast("Upload failed: " + err.message, true); }
  });
  input.addEventListener("change", () => select(input.value.trim()));

  refresh();
  return el("div", { class: "bg-picker" },
    el("div", { class: "bg-actions" },
      el("button", { type: "button", class: "btn small", onclick: () => fileInput.click() }, "Upload image…"),
      el("button", { type: "button", class: "btn small", onclick: () => select("") }, "No background"),
    ),
    fileInput,
    gallery,
  );
}

function editSettings() {
  const values = { ...cfg, weather: { ...cfg.weather, location: cfg.weather?.label || cfg.weather?.name || "" } };
  openForm("Settings", [
    { key: "title", label: "Title" },
    { key: "subtitle", label: "Subtitle" },
    { key: "theme.style", label: "Interface style", type: "select", default: "normal",
      options: [["normal", "Normal"], ["glass", "Glass (translucent, Apple-style)"]],
      hint: "The default for everyone. The ✨ button switches style on a single device." },
    { key: "theme.mode", label: "Theme", type: "select", default: "auto", options: [["auto", "Follow system"], ["light", "Light"], ["dark", "Dark"]] },
    { key: "theme.accent", label: "Accent colour", type: "color", default: "#6366f1" },
    { key: "theme.tileSize", label: "Tile size", type: "select", default: "medium", options: [["small", "Small"], ["medium", "Medium"], ["large", "Large"]] },
    { key: "theme.background", label: "Background image", placeholder: "https://… or upload an image", extra: backgroundPicker,
      hint: "Upload a JPEG, PNG, GIF, WebP or AVIF from this device (max 20 MB), pick one you uploaded before, or paste an image URL." },
    { key: "weather.location", label: "Weather location", placeholder: "London",
      hint: "A city, optionally with region or country (e.g. “Portland, Oregon”), or “lat, lon”." },
    { key: "weather.units", label: "Weather units", type: "select", default: "metric", options: [["metric", "Metric (°C, km/h)"], ["imperial", "Imperial (°F, mph)"]] },
    { key: "search.engine", label: "Search engine URL", default: "https://duckduckgo.com/?q=", hint: "The query is appended, e.g. https://www.google.com/search?q=" },
    { key: "statusInterval", label: "Status check interval (seconds)", type: "number", default: 30 },
    { key: "weather.enabled", label: "Show weather", type: "checkbox", default: false },
    { key: "system.enabled", label: "Show system stats (CPU, temperature, RAM, disk) for this server", type: "checkbox", default: false },
    { key: "rss.enabled", label: "Show News (RSS) tab", type: "checkbox", default: false },
    { key: "upcoming.enabled", label: "Show Upcoming tab (calendar from your Sonarr / Radarr / Lidarr / Readarr / Magarr widgets)", type: "checkbox", default: false },
    { key: "showUptime", label: "Show 24-hour uptime bars on tiles", type: "checkbox" },
    { key: "search.enabled", label: "Show search bar", type: "checkbox" },
    { key: "showClock", label: "Show clock", type: "checkbox" },
    { key: "showGreeting", label: "Show greeting", type: "checkbox" },
    { key: "openInNewTab", label: "Open services in a new tab", type: "checkbox" },
  ], values, {
    onSave: async (v) => {
      const { location, ...wx } = v.weather;
      const prev = cfg.weather || {};
      let place = {};
      if (location && location !== (prev.label || prev.name)) {
        place = await resolveLocation(location); // throws → dialog stays open
      } else if (!location) {
        if (wx.enabled) throw new Error("Enter a location to show the weather");
        place = { name: "", label: "", latitude: null, longitude: null };
      }
      cfg = {
        ...cfg, ...v,
        theme: { ...cfg.theme, ...v.theme },
        search: { ...cfg.search, ...v.search },
        weather: { ...prev, ...wx, ...place },
        rss: { ...cfg.rss, ...v.rss },
        upcoming: { ...cfg.upcoming, ...v.upcoming },
        system: { ...cfg.system, ...v.system },
      };
      try { localStorage.removeItem("uiStyle"); } catch { /* storage unavailable */ } // show the new default here too
      save();
      startStatusPolling();
      startSystemPolling();
      refreshHistory(); // uptime bars appear (or go) straight away
    },
  });
}

function editJson() {
  openForm("Edit raw config", [{ key: "json", label: "config.json", type: "textarea" }], { json: JSON.stringify(cfg, null, 2) }, {
    onSave: (v) => {
      cfg = normaliseConfig(JSON.parse(v.json)); // throws → shown as toast, dialog stays open
      save();
    },
  });
}

function exportConfig() {
  const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "switchboard-config.json" });
  a.click();
  URL.revokeObjectURL(a.href);
}

async function importConfig(e) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const parsed = normaliseConfig(JSON.parse(await file.text()));
    if (!confirm("Replace the current dashboard with this file?")) return;
    cfg = parsed;
    activeTab = 0;
    save();
  } catch (err) {
    toast("Import failed: " + err.message, true);
  }
}

function toggleEdit() {
  const on = document.body.classList.toggle("editing");
  render();
  toast(on ? "Edit mode: click a tile to edit, drag to reorder or onto a tab" : "Edit mode off");
}

// ---------- boot ----------

async function init() {
  [cfg, meta] = await Promise.all([
    api("GET", "/api/config"),
    api("GET", "/api/meta").catch(() => meta),
    loadWidgetPresets(),
  ]);
  normaliseConfig(cfg);

  let saved = null;
  try { saved = localStorage.getItem("activeTab"); } catch { /* storage unavailable */ }
  const fromHash = tabIndexBySlug(decodeURIComponent(location.hash.slice(1)));
  activeTab = fromHash !== -1 ? fromHash : Math.max(0, tabIndexBySlug(saved));

  render();
  tickClock();
  setInterval(tickClock, 1000);
  startStatusPolling();
  startSystemPolling();
  refreshWidgets();
  setInterval(refreshWidgets, 30 * 1000);
  refreshWeather();
  setInterval(refreshWeather, WEATHER_REFRESH_MS);
  if (isRssTab(activeTab)) loadRss();
  if (isUpcomingTab(activeTab)) loadUpcoming();
  setInterval(() => {
    if (isRssTab(activeTab)) loadRss();
    if (isUpcomingTab(activeTab)) loadUpcoming();
  }, 60 * 1000);
  refreshHistory();
  setInterval(refreshHistory, 60 * 1000);

  $("#search").addEventListener("submit", onSearchSubmit);
  $("#searchInput").addEventListener("input", render);
  $("#editToggle").addEventListener("click", toggleEdit);
  $("#uiToggle").addEventListener("click", toggleUiStyle);
  $("#openSettings").addEventListener("click", editSettings);
  $("#openAlerts").addEventListener("click", editAlerts);
  $("#openJson").addEventListener("click", editJson);
  $("#exportCfg").addEventListener("click", exportConfig);
  $("#importCfg").addEventListener("change", importConfig);

  window.addEventListener("hashchange", () => {
    const ti = tabIndexBySlug(decodeURIComponent(location.hash.slice(1)));
    if (ti !== -1 && ti !== activeTab) selectTab(ti);
  });

  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
    if (typing || $("#modal").open || e.metaKey || e.ctrlKey || e.altKey) {
      if (e.key === "Escape" && document.activeElement === $("#searchInput")) {
        $("#searchInput").value = "";
        $("#searchInput").blur();
        render();
      }
      return;
    }
    if (e.key === "/") { e.preventDefault(); $("#searchInput").focus(); }
    else if (e.key.toLowerCase() === "e") toggleEdit();
    else if (e.key.toLowerCase() === "g") toggleUiStyle();
    else if (/^[1-9]$/.test(e.key) && Number(e.key) <= allTabs().length) selectTab(Number(e.key) - 1);
  });
}

init().catch((e) => {
  document.body.append(el("p", { class: "wrap empty" }, "Could not load config: " + e.message));
});
