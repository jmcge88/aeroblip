/* Flight board tablet frontend: websocket-driven, auto-switching views.
   Pages mirror the ESP display: emergency -> air (spotlight/nearby) ->
   departures -> arrivals, with takeovers, rotation, and manual swipes. */

const SPOTLIGHT_LINGER_MS = 15_000; // keep spotlight after plane leaves the ring
const RADAR_LINGER_MS = 30_000;     // air page stays in rotation this long after sky clears
const PAGE_ROTATE_MS = 30_000;      // rotation slot per page (ESP BOARD_FLIP_MS)
const ALERT_ALTERNATE_MS = 15_000;  // 7700 + overhead both active: alternate views
const GLOBAL_ALERT_TAKEOVER_MS = 120_000; // far-away 7700 pins this long, then joins rotation
const MAX_CARDS = 5;                // aircraft cards that fit on screen
const MAX_BOARD_ROWS = 14;
const MAX_EXTRAP_S = 45;            // dead-reckon overhead traffic at most this long
                                    // (~1.5x POLL_SECONDS: covers polls up to 30s)
const ALERT_EXTRAP_S = 90;          // 7700 watch polls every 60s: allow one missed poll

const els = {
  viewTitle: document.getElementById("view-title"),
  clock: document.getElementById("clock"),
  emergencyView: document.getElementById("emergency-view"),
  emBanner: document.getElementById("em-banner"),
  emInfo: document.getElementById("em-info"),
  spotlightView: document.getElementById("spotlight-view"),
  radarView: document.getElementById("radar-view"),
  boardView: document.getElementById("board-view"),
  aircraftList: document.getElementById("aircraft-list"),
  radarEmpty: document.getElementById("radar-empty"),
  boardDirection: document.getElementById("board-direction"),
  boardAirport: document.getElementById("board-airport"),
  boardRows: document.getElementById("board-rows"),
  statusLine: document.getElementById("status-line"),
  mockBadge: document.getElementById("mock-badge"),
  flyoverBtn: document.getElementById("flyover-btn"),
  emBtn: document.getElementById("em-btn"),
  pageDots: document.getElementById("page-dots"),
  flipCount: document.getElementById("flip-count"),
};

let overheadRaw = { aircraft: [], overhead_count: 0 }; // as received from the server
let overhead = overheadRaw;   // dead-reckoned view, rebuilt from raw each render
let board = { arrivals: [], departures: [] };
let alertsRaw = { aircraft: [] }; // global squawk-7700 watch (worldwide)
let alerts = alertsRaw;
let overheadLoaded = false;   // first payload received: empty now means CLEAR SKIES
let accessDenied = false;     // server 403'd us (REQUIRE_DEVICE_TOKEN without a token)
let lastTraffic = 0;          // timestamp of last non-empty radar snapshot
let lastOverhead = 0;         // timestamp of last aircraft inside the overhead ring
let spotlightHex = null;      // sticky spotlight: don't flip between overhead planes

/* Dead reckoning between polls: project each aircraft along its last known
   track at its last known ground speed, and tick altitude by its climb rate,
   so the 1 Hz render loop shows motion instead of a frozen 10-20 s snapshot.
   Positions from adsb.lol are already pos_age_s seconds old at poll time, so
   this is on average *more* accurate than drawing the raw fix - the only
   time it's wrong is mid-turn (a few hundred metres worst case at approach
   speeds), and every real poll snaps it back. Capped so a dropped websocket
   or a provider stand-down doesn't ghost-glide planes off the map. */
function extrapolate(a, updated, maxAgeS, ringNm) {
  if (!updated || a.lat == null || a.lon == null) return a;
  const age = Math.min(Math.max(Date.now() / 1000 - updated + (a.pos_age_s || 0), 0), maxAgeS);
  if (age < 0.5) return a;
  const out = { ...a };
  if (a.ground_speed_kt > 50 && a.track != null) {
    const dNm = a.ground_speed_kt * age / 3600;
    const rad = (a.track * Math.PI) / 180;
    out.lat = a.lat + (dNm * Math.cos(rad)) / 60;
    out.lon = a.lon + (dNm * Math.sin(rad)) / (60 * Math.cos((a.lat * Math.PI) / 180));
    if (a.distance_nm != null && a.bearing_from_home != null) {
      // Move the home->aircraft vector in NM (flat earth is fine at <100 NM)
      const brg = (a.bearing_from_home * Math.PI) / 180;
      const x = a.distance_nm * Math.sin(brg) + dNm * Math.sin(rad);
      const y = a.distance_nm * Math.cos(brg) + dNm * Math.cos(rad);
      out.distance_nm = Math.hypot(x, y);
      out.bearing_from_home = ((Math.atan2(x, y) * 180) / Math.PI + 360) % 360;
      if (ringNm != null) out.overhead = out.distance_nm <= ringNm;
    }
  }
  if (a.altitude_ft != null && a.vertical_rate_fpm != null)
    out.altitude_ft = Math.max(0, a.altitude_ft + (a.vertical_rate_fpm * age) / 60);
  return out;
}

function liveSnapshot(raw, maxAgeS) {
  if (!raw.aircraft?.length) return raw;
  const aircraft = raw.aircraft.map((a) =>
    extrapolate(a, raw.updated, maxAgeS, raw.overhead_radius_nm));
  aircraft.sort((p, q) => (p.distance_nm ?? 9e9) - (q.distance_nm ?? 9e9));
  const out = { ...raw, aircraft };
  if ("overhead_count" in raw)
    out.overhead_count = aircraft.filter((a) => a.overhead).length;
  return out;
}

/* Only touch the DOM when content actually changed - innerHTML rewrites
   re-create <img> tags and replay animations, which reads as flicker. */
function setHTML(el, html) {
  if (el.__html !== html) { el.__html = html; el.innerHTML = html; }
}

/* Every string rendered below originates upstream (adsb.lol positions, adsbdb
   or standing-data enrichment, AeroDataBox board rows, bigdatacloud place
   names) and lands in innerHTML, so none of it may reach the DOM raw. esc() is
   for text nodes and quoted attribute values; escUrl() additionally refuses
   anything that isn't a plain http(s) or root-relative URL, which is what keeps
   a hostile photo URL from closing the src attribute and adding an onerror. */
function esc(v) {
  if (v == null) return "";
  return String(v).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function escUrl(url) {
  if (!url) return "";
  const s = String(url);
  // Absolute http(s), or genuinely root-relative - "/" but not "//host" (which
  // is protocol-relative, i.e. someone else's host wearing a local disguise).
  if (!/^(https?:\/\/|\/(?!\/))[^\s"'<>\\]*$/i.test(s)) return "";
  return esc(s);
}

/* ---------- clock ---------- */
setInterval(() => {
  els.clock.textContent = new Date().toLocaleTimeString([], { hour12: false });
}, 1000);

/* ---------- screen wake lock (needs HTTPS or localhost; fails silently) ---------- */
async function keepAwake() {
  try {
    if ("wakeLock" in navigator) await navigator.wakeLock.request("screen");
  } catch { /* not supported / not permitted */ }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") keepAwake();
});
keepAwake();

/* ---------- view location (URL query > saved settings > server default) ----------
   The server polls one sky per distinct location; this page can watch any of
   them. ?lat=&lon=&radius=&area=&airport= in the URL wins (shareable links);
   otherwise settings saved from the location panel (localStorage) apply. */
// "token" rides along for servers running REQUIRE_DEVICE_TOKEN=true
const LOC_KEYS = ["lat", "lon", "radius", "area", "airport", "token"];
// Keys safe to leave in the address bar (everything except the credential)
const URL_KEYS = LOC_KEYS.filter((k) => k !== "token");

/* A token in the address bar leaks: into browser history, into every server's
   access log, and into the Referer header sent to third-party origins (the map
   tile CDN). Kiosks still need to *accept* ?token=, so take it once at load,
   keep it in memory, and scrub it out of the visible URL immediately. */
let urlToken = null;
(function scrubTokenFromUrl() {
  const qs = new URLSearchParams(location.search);
  const t = qs.get("token");
  if (!t) return;
  urlToken = t;
  qs.delete("token");
  const rest = qs.toString();
  history.replaceState(null, "", rest ? `?${rest}` : location.pathname);
})();

function locSettings() {
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem("viewLocation")) || {}; }
  catch { stored = {} }

  const qs = new URLSearchParams(location.search);
  let o;
  if (URL_KEYS.some((k) => qs.get(k))) {
    o = {};                                     // URL location wins, as documented
    for (const k of URL_KEYS) if (qs.get(k)) o[k] = qs.get(k);
  } else {
    o = { ...stored };
  }
  // The token is resolved separately from the location: it never rides in the
  // URL, so it comes from the ?token= scrubbed at load or from saved settings.
  const token = urlToken || stored.token;
  if (token) o.token = token;
  return o;
}

function locQuery() {
  const o = locSettings();
  const qs = new URLSearchParams();
  for (const k of LOC_KEYS) if (o[k]) qs.set(k, o[k]);
  const s = qs.toString();
  return s ? `?${s}` : "";
}

/* ---------- websocket ---------- */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws${locQuery()}`);

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "overhead") {
      overheadRaw = msg.data;
      overheadLoaded = true;
      if (msg.data.aircraft.length > 0) lastTraffic = Date.now();
      if (msg.data.overhead_count > 0) lastOverhead = Date.now();
      announceOverhead(msg.data);
      processWatchEvents(msg.data.watch_events || []);
      render();
    } else if (msg.type === "board") {
      board = msg.data;
      els.mockBadge.classList.toggle("hidden", !board.mock);
      render();
    } else if (msg.type === "alerts") {
      announceAlerts(msg.data);
      alertsRaw = msg.data;
      render();
    }
  };
  ws.onopen = () => {
    accessDenied = false;
    const o = locSettings();
    els.statusLine.textContent = o.lat ? `live @ ${o.lat}, ${o.lon}` : "live";
  };
  ws.onclose = async () => {
    els.statusLine.textContent = "reconnecting…";
    // A rejected handshake looks identical to a network blip from here, and
    // an auth lockout must not masquerade as "CLEAR SKIES" - probe and say so
    try {
      const r = await fetch("/api/overhead" + locQuery());
      if (r.status === 403) {
        accessDenied = true;
        els.statusLine.textContent =
          "access denied - this server requires a device token: set it in the ⌖ panel";
        render();
      }
    } catch { /* server unreachable - plain reconnect loop */ }
    setTimeout(connect, 3000);
  };
  ws.onerror = () => ws.close();
}
connect();

/* ---------- squawk 7500/7600/7700 alerts ---------- */
function isAlert(a) {
  if (["7500", "7600", "7700"].includes(a.squawk)) return true;
  const e = (a.emergency || "").toLowerCase();
  return e !== "" && e !== "none" && e !== "lifeguard";
}

function alertAircraft() {
  // Local traffic first (has full enrichment), then the global 7700 watch
  return overhead.aircraft.find(isAlert) ?? alerts.aircraft?.[0] ?? null;
}

function alertLabel(a) {
  if (a.squawk === "7700") return "GENERAL EMERGENCY";
  if (a.squawk === "7600") return "RADIO FAILURE";
  if (a.squawk === "7500") return "UNLAWFUL INTERFERENCE";
  return (a.emergency || "EMERGENCY").toUpperCase();
}

/* ---------- page rotation (port of the ESP display state machine) ---------- */
// Force a view for demos/testing: ?view=spotlight | nearby | board | emergency
const FORCED_VIEW = new URLSearchParams(location.search).get("view");

let currentPage = "air";      // "emergency" | "air" (spotlight) | "nearby" | "departures" | "arrivals"
let lastFlipAt = Date.now();  // rotation slot timer (reset by manual input)
let lastInputAt = 0;          // manual choices override the takeover snap for one slot
let wasTakeover = false;
let alertHexSeen = null;      // demote timer identity for global (far-away) alerts
let alertSince = 0;

function boardRows(which) {
  return board.unavailable ? [] : (board[which] || []);
}

function spotlightDue() {
  return overhead.aircraft.length > 0 &&
    (overhead.overhead_count > 0 || Date.now() - lastOverhead < SPOTLIGHT_LINGER_MS);
}

// Pages reachable by swipe / dots (ESP buildPages). "nearby" is its own page,
// not a fallback layout of "air": in busy airspace the ring is never empty,
// so a shared page would pin the spotlight and make nearby unreachable.
function buildPages() {
  const pages = [];
  if (alertAircraft()) pages.push("emergency");
  if (spotlightDue()) pages.push("air");
  pages.push("nearby");
  if (boardRows("departures").length) pages.push("departures");
  if (boardRows("arrivals").length) pages.push("arrivals");
  return pages;
}

// Decide the page to show. Returns seconds until the next automatic change
// (global-alert demotion, takeover return, or rotation), or null if none due.
function choosePage() {
  const now = Date.now();
  const local = overhead.aircraft.find(isAlert) ?? null;
  const globalA = local ? null : (alerts.aircraft?.[0] ?? null);
  const anyAlert = local ?? globalA;
  let alert = !!anyAlert;
  let holdLeft = null;
  if (globalA) {
    // Global alerts can stay active for hours: take over for the first two
    // minutes, then join the rotation instead of pinning the screen
    if (globalA.hex !== alertHexSeen) { alertHexSeen = globalA.hex; alertSince = now; }
    const held = now - alertSince;
    if (held > GLOBAL_ALERT_TAKEOVER_MS) alert = false; // demoted
    else holdLeft = Math.floor((GLOBAL_ALERT_TAKEOVER_MS - held) / 1000) + 1;
  } else if (!anyAlert) {
    alertHexSeen = null;
  }

  const spotlight = spotlightDue();
  let takeover = null;
  if (alert && spotlight)
    takeover = Math.floor(now / ALERT_ALTERNATE_MS) % 2 ? "air" : "emergency";
  else if (alert) takeover = "emergency";
  else if (spotlight) takeover = "air";

  if (takeover) {
    const onTakeoverPage = currentPage === "air" || currentPage === "emergency";
    const manualFresh = now - lastInputAt < PAGE_ROTATE_MS;
    if (!wasTakeover) {
      wasTakeover = true;
      lastFlipAt = now;
      currentPage = takeover;
      return holdLeft;
    }
    if (currentPage === takeover) return holdLeft;
    if (onTakeoverPage && !manualFresh) {
      currentPage = takeover;
      return holdLeft;
    }
    // Manual choice keeps its slot - the timer then brings the takeover back
    if (now - lastFlipAt >= PAGE_ROTATE_MS) {
      currentPage = takeover;
      lastFlipAt = now;
      return holdLeft;
    }
    return Math.floor((PAGE_ROTATE_MS - (now - lastFlipAt)) / 1000) + 1;
  }
  wasTakeover = false;

  // Rotate through every page with content (a demoted alert stays in the cycle)
  const rot = [];
  if (anyAlert) rot.push("emergency");
  if (overhead.aircraft.length > 0 || (lastTraffic && now - lastTraffic < RADAR_LINGER_MS))
    rot.push("nearby");
  if (boardRows("departures").length) rot.push("departures");
  if (boardRows("arrivals").length) rot.push("arrivals");
  if (!rot.length) { currentPage = "nearby"; return null; } // CLEAR SKIES placeholder

  const cur = rot.indexOf(currentPage);
  if (now - lastFlipAt >= PAGE_ROTATE_MS) {
    currentPage = cur < 0 ? rot[0] : rot[(cur + 1) % rot.length];
    lastFlipAt = now;
  }
  return rot.length >= 2 || cur < 0
    ? Math.floor((PAGE_ROTATE_MS - (now - lastFlipAt)) / 1000) + 1
    : null;
}

/* ---------- manual input: swipe or dots get a fresh 30s slot, rotation continues ---------- */
function selectPage(page) {
  currentPage = page;
  lastFlipAt = Date.now();
  lastInputAt = lastFlipAt;
  render();
}

function switchPage(delta) {
  const pages = buildPages();
  const i = Math.max(pages.indexOf(currentPage), 0);
  selectPage(pages[(i + delta + pages.length) % pages.length]);
}

let touchStart = null;
document.addEventListener("touchstart", (e) => {
  touchStart = { x: e.touches[0].clientX, y: e.touches[0].clientY };
}, { passive: true });
document.addEventListener("touchend", (e) => {
  if (!touchStart) return;
  const dx = e.changedTouches[0].clientX - touchStart.x;
  const dy = e.changedTouches[0].clientY - touchStart.y;
  touchStart = null;
  if (Math.abs(dx) > 70 && Math.abs(dx) > 2 * Math.abs(dy)) switchPage(dx < 0 ? +1 : -1);
}, { passive: true });

els.pageDots.addEventListener("click", (e) => {
  const p = e.target.closest("[data-page]")?.dataset.page;
  if (p) selectPage(p);
});

setInterval(render, 1000); // drive rotation, countdowns and linger without new data
render(); // first paint immediately - the loading state must not wait a tick

els.flyoverBtn.addEventListener("click", () => {
  fetch("/api/demo/flyover", { method: "POST" });
});

els.emBtn.addEventListener("click", () => {
  fetch("/api/demo/emergency", { method: "POST" });
});

/* ---------- rendering ---------- */
function render() {
  // Rebuild the dead-reckoned views from the raw snapshots every tick - the
  // 1 Hz interval below is what makes the pages look live between polls.
  overhead = liveSnapshot(overheadRaw, MAX_EXTRAP_S);
  alerts = liveSnapshot(alertsRaw, ALERT_EXTRAP_S);
  let page, flipIn = null;
  if (["spotlight", "nearby", "board", "emergency"].includes(FORCED_VIEW)) {
    page = { spotlight: "air", nearby: "nearby", board: "departures", emergency: "emergency" }[FORCED_VIEW];
    currentPage = page;
  } else {
    flipIn = choosePage();
    page = currentPage;
  }

  let view;
  if (page === "emergency") view = "emergency";
  // The air page gracefully degrades to the nearby layout if the spotlight
  // expired between rotation ticks (plane left the ring, linger ran out)
  else if (page === "air") view = spotlightDue() ? "spotlight" : "nearby";
  else if (page === "nearby") view = "nearby";
  else view = "board";
  if (FORCED_VIEW === "spotlight") view = "spotlight";
  else if (FORCED_VIEW === "nearby") view = "nearby";

  els.emergencyView.classList.toggle("hidden", view !== "emergency");
  els.spotlightView.classList.toggle("hidden", view !== "spotlight");
  els.radarView.classList.toggle("hidden", view !== "nearby");
  els.boardView.classList.toggle("hidden", view !== "board");

  if (view === "emergency") {
    const a = alertAircraft();
    els.viewTitle.textContent = a?.squawk ? `SQUAWK ${a.squawk}` : "EMERGENCY";
    renderEmergency(a);
  } else if (view === "spotlight") {
    els.viewTitle.textContent = "OVERHEAD";
    renderSpotlight();
  } else if (view === "nearby") {
    els.viewTitle.textContent = "NEARBY TRAFFIC";
    renderRadar();
  } else {
    els.viewTitle.textContent = board.airport ? `${board.airport.iata} AIRPORT` : "AIRPORT";
    renderBoard(page === "departures");
  }
  renderFooterNav(flipIn);
  const upd = overhead.updated ? new Date(overhead.updated * 1000).toLocaleTimeString([], { hour12: false }) : "–";
  els.statusLine.textContent =
    `${overhead.provider || "?"} · ${overhead.overhead_count ?? 0} overhead (${overhead.overhead_radius_nm ?? "?"} NM) · ${overhead.aircraft.length} within ${overhead.area_radius_nm ?? "?"} NM · updated ${upd}`;
  els.flyoverBtn.classList.toggle("hidden", overhead.provider !== "demo");
  els.emBtn.classList.toggle("hidden", overhead.provider !== "demo");
}

// Page dots + ">> Ns" countdown, matching the ESP footer
function renderFooterNav(flipIn) {
  const pages = buildPages();
  const dots = pages.length < 2 ? "" : pages.map((p) =>
    `<button class="dot${p === currentPage ? " on" : ""}" data-page="${p}" aria-label="${p}"></button>`).join("");
  setHTML(els.pageDots, dots);
  els.flipCount.textContent = flipIn != null ? `>> ${flipIn}S` : "";
}

function arrowFor(track) {
  // CSS-rotated arrow glyph pointing in direction of travel
  return track == null ? "•" : "↑";
}

/* ---------- location panel ---------- */
const locEls = {
  btn: document.getElementById("loc-btn"),
  panel: document.getElementById("loc-panel"),
  latlon: document.getElementById("loc-latlon"),
  radius: document.getElementById("loc-radius"),
  area: document.getElementById("loc-area"),
  airport: document.getElementById("loc-airport"),
  token: document.getElementById("loc-token"),
  gps: document.getElementById("loc-gps"),
  save: document.getElementById("loc-save"),
  clear: document.getElementById("loc-clear"),
  note: document.getElementById("loc-note"),
};

locEls.btn.onclick = () => {
  const o = locSettings();
  locEls.latlon.value = o.lat && o.lon ? `${o.lat}, ${o.lon}` : "";
  locEls.radius.value = o.radius ?? "";
  locEls.area.value = o.area ?? "";
  locEls.airport.value = o.airport ?? "";
  locEls.token.value = o.token ?? "";
  locEls.note.textContent = "";
  locEls.panel.classList.toggle("hidden");
};

locEls.gps.onclick = () => {
  // Geolocation needs a secure context - on plain LAN HTTP fall back to paste
  if (!("geolocation" in navigator) || !window.isSecureContext) {
    locEls.note.textContent =
      "GPS needs HTTPS - long-press your spot in Google Maps and paste the coordinates.";
    return;
  }
  locEls.note.textContent = "locating…";
  navigator.geolocation.getCurrentPosition(
    (p) => {
      locEls.latlon.value =
        `${p.coords.latitude.toFixed(6)}, ${p.coords.longitude.toFixed(6)}`;
      locEls.note.textContent = "";
    },
    (e) => { locEls.note.textContent = `No fix (${e.message}) - paste coordinates instead.`; },
    { enableHighAccuracy: true, timeout: 15000 },
  );
};

locEls.save.onclick = () => {
  const o = {};
  const raw = locEls.latlon.value.trim();
  const m = raw.match(/^(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)$/);
  if (m) {
    o.lat = m[1];
    o.lon = m[2];
  } else if (raw) {
    locEls.note.textContent = "Location must look like: -33.8688, 151.2093";
    return;
  }
  if (locEls.radius.value) o.radius = locEls.radius.value;
  if (locEls.area.value) o.area = locEls.area.value;
  if (locEls.airport.value.trim()) o.airport = locEls.airport.value.trim().toUpperCase();
  if (locEls.token.value.trim()) o.token = locEls.token.value.trim();
  localStorage.setItem("viewLocation", JSON.stringify(o));
  // The token stays out of the URL - locSettings() picks it up from storage
  const qs = new URLSearchParams();
  for (const k of URL_KEYS) if (o[k]) qs.set(k, o[k]);
  const s = qs.toString();
  // Reload so the websocket reconnects against the new sky
  if (s) location.search = s;
  else if (location.search) location.search = "";
  else location.reload();
};

locEls.clear.onclick = () => {
  localStorage.removeItem("viewLocation");
  if (location.search) location.search = "";
  else location.reload();
};

/* Photos and logos: probe each URL once, off-DOM. An <img> is only rendered
   for a URL that has actually loaded (so it paints instantly from cache), and
   failed URLs are never rendered or retried - dead links used to flash in and
   out of the cards on every poll as innerHTML rewrites re-attempted them. */
const imgOk = new Set();
const imgSeen = new Set();
function probedImg(url, cls) {
  if (!url) return "";
  const safe = escUrl(url);
  if (!safe) return ""; // not a plain http(s)/relative URL - never render it
  if (imgOk.has(url)) return `<img class="${esc(cls)}" src="${safe}" alt="">`;
  if (!imgSeen.has(url)) {
    imgSeen.add(url);
    const probe = new Image();
    probe.onload = () => { imgOk.add(url); render(); };
    // Transient failures may retry later; retries stay off-DOM so no flicker
    probe.onerror = () => setTimeout(() => imgSeen.delete(url), 300_000);
    probe.src = url;
  }
  return "";
}

function logoImg(iata, cls) {
  if (!iata || !/^[A-Z0-9]{2}$/.test(iata)) return "";
  // Served from our own cache (see /api/logo) - upstream source is configurable
  return probedImg(`/api/logo/${iata}`, cls);
}

/* Seconds until the aircraft enters the overhead ring, or null if it won't.
   Geometry: project the home-relative position onto the aircraft's track. */
function etaToOverhead(a) {
  if (a.overhead || a.distance_nm == null || a.bearing_from_home == null
      || a.track == null || !(a.ground_speed_kt > 50)) return null;
  const ringNm = overhead.overhead_radius_nm || 5;
  const toHome = (a.bearing_from_home + 180) % 360;         // bearing aircraft -> home
  const delta = ((a.track - toHome + 540) % 360) - 180;     // signed angle off that line
  const rad = (delta * Math.PI) / 180;
  const along = a.distance_nm * Math.cos(rad);              // NM until closest approach
  const cross = Math.abs(a.distance_nm * Math.sin(rad));    // miss distance NM
  if (along <= 0 || cross > ringNm) return null;            // flying away, or will miss
  const toRing = along - Math.sqrt(ringNm * ringNm - cross * cross);
  if (toRing <= 0) return null;
  // No staleness correction here: position/distance are already dead-reckoned
  // to "now" by extrapolate(), so the raw ETA is current.
  const secs = toRing / (a.ground_speed_kt / 3600);
  return secs > 2 && secs < 900 ? secs : null;              // only if under 15 min
}

function fmtEta(secs) {
  const m = Math.floor(secs / 60), s = Math.round(secs % 60);
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

/* ---------- emergency view (squawk alert + map) ---------- */
let emMap = null, emPlane = null, emHome = null, emRing = null, emTrail = null;
let emTrailHex = null;

/* Great-circle destination point: recover the home location from the
   aircraft's position, distance and bearing-from-home (nothing new leaks -
   it's already derivable from the API response). */
function destPoint(lat, lon, bearingDeg, distNm) {
  const R = 3440.065; // earth radius, NM
  const d = distNm / R, brg = (bearingDeg * Math.PI) / 180;
  const p1 = (lat * Math.PI) / 180, l1 = (lon * Math.PI) / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(brg));
  const l2 = l1 + Math.atan2(Math.sin(brg) * Math.sin(d) * Math.cos(p1),
                             Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [(p2 * 180) / Math.PI, (l2 * 180) / Math.PI];
}

function planeDivIcon(track) {
  return L.divIcon({
    className: "em-plane-icon",
    iconSize: [30, 30],
    html: `<svg viewBox="0 0 30 30" style="transform:rotate(${Math.round(track ?? 0)}deg)">
             <path d="M15,3 L24,25 L15,19.5 L6,25 Z" fill="#ff5c5c" stroke="#0a0e14" stroke-width="1.5"/>
           </svg>`,
  });
}

function renderEmergency(a) {
  if (!a && FORCED_VIEW === "emergency") a = overhead.aircraft[0]; // test mode stand-in
  if (!a) { setHTML(els.emInfo, ""); setHTML(els.emBanner, "ALERT CLEARED"); return; }

  const squawkTxt = a.squawk ? `SQUAWK ${esc(a.squawk)} · ` : "";
  setHTML(els.emBanner, `⚠ ${squawkTxt}${esc(alertLabel(a))} ⚠`);

  const cs = esc(a.callsign || a.registration || a.hex);
  const airline = esc(a.airline?.airline ?? a.route?.airline ?? "");
  const route = a.route
    ? `${esc(a.route.origin ?? "?")} <span class="arrow">→</span> ${esc(a.route.destination ?? "?")}`
    : esc(a.description || a.type || "");
  const alt = a.altitude_ft != null ? `${Math.round(a.altitude_ft).toLocaleString()} ft` : "–";
  const spd = a.ground_speed_kt != null ? `${Math.round(a.ground_speed_kt)} kt` : "–";
  const dist = a.distance_nm != null
    ? (a.distance_nm > 100
        ? `${Math.round(a.distance_nm).toLocaleString()} NM`
        : `${a.distance_nm.toFixed(1)} NM`)
    : "–";
  const phase = a.phase && a.phase !== "level"
    ? ` <span class="ac-phase-${esc(a.phase)}">${esc(a.phase.toUpperCase())}</span>` : "";
  const heading = a.heading_cardinal
    ? `${esc(a.heading_cardinal)}${a.track != null ? ` (${Math.round(a.track)}°)` : ""}` : "–";

  // Values here are pre-escaped where they came from upstream; alt/spd/dist are
  // locally formatted numbers, and phase/heading carry intentional markup.
  const factList = [
    ["AIRCRAFT", esc(a.description || a.type || "–")],
    ["REGISTRATION", esc(a.registration ?? "–")],
    ["ALTITUDE", alt + phase],
    ["SPEED", spd],
    ["HEADING", heading],
    ["DISTANCE", dist],
  ];
  if (a.place) factList.splice(2, 0, ["LOCATION", esc(a.place)]);
  const facts = factList
    .map(([k, v]) => `<div class="sp-fact"><label>${k}</label><span>${v}</span></div>`).join("");

  setHTML(els.emInfo, `
    <div class="em-airline">${airline}</div>
    <div class="em-callsign">${cs}</div>
    <div class="em-route">${route}</div>
    <div class="sp-facts">${facts}</div>`);

  updateEmergencyMap(a);
}

function updateEmergencyMap(a) {
  if (typeof L === "undefined" || a.lat == null || a.lon == null) return;
  const pos = [a.lat, a.lon];
  const ringNm = overhead.overhead_radius_nm || 5;

  if (!emMap) {
    emMap = L.map("em-map", { zoomControl: false, attributionControl: false });
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
                { maxZoom: 12 }).addTo(emMap);
    emPlane = L.marker(pos, { icon: planeDivIcon(a.track) }).addTo(emMap);
    emTrail = L.polyline([], { color: "#ff5c5c", weight: 2, opacity: 0.7 }).addTo(emMap);
  }
  // The container may have been hidden or un-laid-out when the map was
  // created - recheck the real size before fitting, synchronously (rAF
  // doesn't fire in background tabs)
  emMap.invalidateSize(false);
  // Home + ring appear once we can derive the home point
  if (a.distance_nm != null && a.bearing_from_home != null) {
    const home = destPoint(a.lat, a.lon, (a.bearing_from_home + 180) % 360, a.distance_nm);
    if (!emHome) {
      emHome = L.circleMarker(home, { radius: 5, color: "#ffb400", fillOpacity: 1 }).addTo(emMap);
      emRing = L.circle(home, { radius: ringNm * 1852, color: "#ffb400", weight: 1,
                                fill: false, dashArray: "4 4" }).addTo(emMap);
    } else {
      emHome.setLatLng(home);
      emRing.setLatLng(home);
    }
    emMap.fitBounds(L.latLngBounds([pos, home]).pad(0.3), { maxZoom: 11 });
  } else {
    // Global alert far from home: centre on the aircraft with some context
    emMap.setView(pos, (a.distance_nm ?? 0) > 200 ? 6 : 9);
  }

  emPlane.setLatLng(pos);
  emPlane.setIcon(planeDivIcon(a.track));
  if (emTrailHex !== a.hex) { emTrailHex = a.hex; emTrail.setLatLngs([]); }
  emTrail.addLatLng(pos);
}

/* ---------- spotlight (single flight overhead) ---------- */
function planeMarker(a, cls, ringNm) {
  if (a.distance_nm == null || a.bearing_from_home == null) return "";
  const r = Math.min(a.distance_nm / ringNm, 1) * 44;
  const rad = (a.bearing_from_home * Math.PI) / 180;
  const x = (50 + r * Math.sin(rad)).toFixed(1);
  const y = (50 - r * Math.cos(rad)).toFixed(1);
  const rot = Math.round(a.track ?? 0);
  return `<g transform="translate(${x} ${y}) rotate(${rot})">
            <path d="M0,-5 L3.6,4.4 L0,2.2 L-3.6,4.4 Z" class="${cls}"/>
          </g>`;
}

function mapSVG(a) {
  const ringNm = overhead.overhead_radius_nm || 5;
  // Other aircraft inside the map's coverage, drawn dim behind the spotlight
  const others = overhead.aircraft
    .filter((x) => x.hex !== a.hex && x.distance_nm != null && x.distance_nm <= ringNm)
    .map((x) => planeMarker(x, "map-plane-other", ringNm))
    .join("");
  const plane = planeMarker(a, "map-plane", ringNm);
  return `
    <svg viewBox="0 0 100 100">
      <line x1="50" y1="6" x2="50" y2="94" class="map-grid"/>
      <line x1="6" y1="50" x2="94" y2="50" class="map-grid"/>
      <circle cx="50" cy="50" r="44" class="map-ring"/>
      <circle cx="50" cy="50" r="29.3" class="map-ring"/>
      <circle cx="50" cy="50" r="14.7" class="map-ring"/>
      <circle cx="50" cy="50" r="1.6" class="map-home"/>
      <text x="50" y="4.5" class="map-label" text-anchor="middle">N</text>
      <text x="96" y="48" class="map-label" text-anchor="end">${ringNm}NM</text>
      ${others}
      ${plane}
    </svg>`;
}

/* Sticky selection: keep showing the same plane while it's overhead (or while
   lingering after the ring empties) instead of "nearest wins" every poll,
   which flips between planes when two are overhead at once. */
function pickSpotlightAircraft() {
  const list = overhead.aircraft;
  if (list.length === 0) { spotlightHex = null; return null; }
  const anyOverhead = list.some((x) => x.overhead);
  const cur = list.find((x) => x.hex === spotlightHex);
  if (cur && (cur.overhead || !anyOverhead)) return cur;
  const next = list.find((x) => x.overhead) ?? list[0];
  spotlightHex = next.hex;
  return next;
}

function renderSpotlight() {
  const a = pickSpotlightAircraft();
  if (!a) { els.spotlightView.__key = null; setHTML(els.spotlightView, ""); return; }

  const cs = esc(a.callsign || a.registration || a.hex);
  const others = overhead.aircraft.filter((x) => x.overhead && x.hex !== a.hex);
  const airline = esc(a.airline?.airline ?? a.route?.airline ?? a.info?.owner ?? "");
  const logo = logoImg(a.airline?.airline_iata, "sp-logo");
  const route = a.route
    ? `<div class="sp-route-codes">${esc(a.route.origin ?? "?")} <span class="arrow">\u2192</span> ${esc(a.route.destination ?? "?")}</div>
       <div class="sp-route-cities">${esc(a.route.origin_name ?? "")} \u2192 ${esc(a.route.destination_name ?? "")}</div>`
    : `<div class="sp-route-codes sp-route-unknown">ROUTE UNKNOWN</div>`;

  /* Rebuild the identity block (which contains <img> tags) only when the
     flight or its enrichment changes - recreating images every poll makes
     the layout jump while they (re)load or fail. */
  // Some hosts (airport-data.com) serve thumbnails to browsers but block
  // hotlinked full-size images - probedImg falls through to the thumb
  const photo = probedImg(a.info?.photo, "sp-photo")
    || probedImg(a.info?.photo_thumb, "sp-photo");
  const key = `${a.hex}|${!!a.route}|${!!a.airline}|${logo}|${photo}`;
  if (els.spotlightView.__key !== key) {
    els.spotlightView.__key = key;
    els.spotlightView.__html = undefined; // direct innerHTML write invalidates setHTML cache
    els.spotlightView.innerHTML = `
      <div class="spotlight">
        <div class="sp-info">
          <div class="sp-airline">${logo}<span>${airline}</span></div>
          <div class="sp-callsign">${cs}</div>
          <div class="sp-route">${route}</div>
          <div class="sp-eta"></div>
          <div class="sp-facts"></div>
          ${photo}
        </div>
        <div class="sp-map"></div>
        <div class="sp-others"></div>
      </div>`;
  }

  const model = esc(a.info
    ? [a.info.manufacturer, a.info.model].filter(Boolean).join(" ")
    : (a.description || a.type || ""));
  const alt = a.altitude_ft != null ? `${Math.round(a.altitude_ft).toLocaleString()} ft` : "\u2013";
  const spd = a.ground_speed_kt != null ? `${Math.round(a.ground_speed_kt)} kt` : "\u2013";
  const dist = a.distance_nm != null ? `${a.distance_nm.toFixed(1)} NM` : "\u2013";
  const phase = a.phase && a.phase !== "level"
    ? ` <span class="ac-phase-${esc(a.phase)}">${esc(a.phase.toUpperCase())}</span>` : "";
  const heading = a.heading_cardinal
    ? `${esc(a.heading_cardinal)}${a.track != null ? ` (${Math.round(a.track)}\u00b0)` : ""}` : "\u2013";

  const facts = [
    ["AIRCRAFT", model || "\u2013"],
    ["REGISTRATION", esc(a.registration ?? "\u2013")],
    ["ALTITUDE", alt + phase],
    ["SPEED", spd],
    ["HEADING", heading],
    ["DISTANCE", dist],
  ].map(([k, v]) => `<div class="sp-fact"><label>${k}</label><span>${v}</span></div>`).join("");

  // Only the text facts and vector map update each poll - no <img> churn.
  setHTML(els.spotlightView.querySelector(".sp-facts"), facts);
  const eta = etaToOverhead(a);
  const spTags = [
    eta != null ? `OVERHEAD IN ${fmtEta(eta)}` : "",
    a.circling ? `<span class="circling-tag">CIRCLING</span>` : "",
    overhead.sun?.golden ? `<span class="golden-tag">☀ GOLDEN LIGHT</span>` : "",
  ].filter(Boolean).join(" ");
  setHTML(els.spotlightView.querySelector(".sp-eta"), spTags);
  setHTML(els.spotlightView.querySelector(".sp-map"), mapSVG(a));
  // Nothing else overhead: fill the strip with the nearest area traffic instead
  let othersHtml = others.map(otherCard).join("");
  if (!othersHtml) {
    const nearby = overhead.aircraft.filter((x) => x.hex !== a.hex).slice(0, 4);
    if (nearby.length)
      othersHtml = `<div class="sp-others-hdr">ALSO NEARBY (${overhead.aircraft.length} IN AREA)</div>`
        + nearby.map(otherCard).join("");
  }
  setHTML(els.spotlightView.querySelector(".sp-others"), othersHtml);
}

/* Compact card for each additional overhead aircraft (text only - no <img>
   tags, since this re-renders every poll). */
function otherCard(a) {
  const cs = esc(a.callsign || a.registration || a.hex);
  const route = a.route
    ? `${esc(a.route.origin ?? "?")} <span class="arrow">\u2192</span> ${esc(a.route.destination ?? "?")}`
    : esc(a.type || "");
  const alt = a.altitude_ft != null ? `${Math.round(a.altitude_ft).toLocaleString()} ft` : "";
  const dist = a.distance_nm != null ? `${a.distance_nm.toFixed(1)} NM` : "";
  const airline = esc(a.airline?.airline ?? a.route?.airline ?? "");
  return `
    <div class="sp-other">
      <span class="sp-other-cs">${cs}</span>
      <span class="sp-other-route">${route}</span>
      <span class="sp-other-sub">${[airline, alt, dist].filter(Boolean).join(" \u00b7 ")}</span>
    </div>`;
}

function renderRadar() {
  const list = overhead.aircraft.slice(0, MAX_CARDS);
  els.radarEmpty.classList.toggle("hidden", list.length > 0);
  if (!list.length) {
    // Three honest empty states: locked out, still waiting, genuinely clear
    setHTML(els.radarEmpty, accessDenied
      ? '<div class="empty-msg">TOKEN REQUIRED (⌖)</div>'
      : overheadLoaded
        ? '<div class="empty-msg">CLEAR SKIES</div>'
        : '<div class="empty-msg loading"><span class="spinner"></span>LOADING TRAFFIC…</div>');
  }
  setHTML(els.aircraftList, list.map((a) => {
    const cs = esc(a.callsign || a.registration || a.hex);
    const route = a.route
      ? `${esc(a.route.origin ?? "?")} <span class="arrow">→</span> ${esc(a.route.destination ?? "?")}`
      : esc(a.description || a.type || "");
    const alt = a.altitude_ft != null ? `${Math.round(a.altitude_ft).toLocaleString()} ft` : "";
    const spd = a.ground_speed_kt != null ? `${Math.round(a.ground_speed_kt)} kt` : "";
    const dist = a.distance_nm != null ? `${a.distance_nm.toFixed(1)} NM away` : "";
    const phase = a.phase && a.phase !== "level"
      ? `<span class="ac-phase-${esc(a.phase)}">${esc(a.phase.toUpperCase())}</span>` : "";
    const eta = etaToOverhead(a);
    const etaTag = eta != null ? `<span class="ac-eta">OVERHEAD IN ${fmtEta(eta)}</span>` : "";
    const circlingTag = a.circling ? `<span class="circling-tag">CIRCLING</span>` : "";
    const goldenTag = overhead.sun?.golden && (a.overhead || eta != null)
      ? `<span class="golden-tag">☀ GOLDEN</span>` : "";
    const rot = a.track != null ? `transform: rotate(${Math.round(a.track)}deg)` : "";
    const airline = esc(a.airline?.airline ?? "");
    const logo = logoImg(a.airline?.airline_iata, "ac-logo");
    const thumb = probedImg(a.info?.photo_thumb, "ac-photo")
      || probedImg(a.info?.photo, "ac-photo");
    return `
      <div class="aircraft-card">
        ${logo || '<span class="ac-logo ac-logo-ph">✈</span>'}
        <div class="ac-main">
          <span class="ac-callsign">${cs}</span>
          <span class="ac-route">${route}</span>
        </div>
        <div class="ac-sub">
          ${airline ? `<span>${airline}</span>` : ""}
          <span>${esc(a.type ?? "")} ${a.registration ? "· " + esc(a.registration) : ""}</span>
          <span>${alt}</span><span>${spd}</span><span>${dist}</span>${phase}${etaTag}${circlingTag}${goldenTag}
        </div>
        <div class="ac-side">
          ${thumb}
          <div class="ac-compass">
            <span class="ac-arrow" style="${rot}">${arrowFor(a.track)}</span>
            <span class="ac-dir">${esc(a.heading_cardinal ?? "")}</span>
          </div>
        </div>
      </div>`;
  }).join(""));
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso.slice(11, 16) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

function statusClass(status) {
  const s = (status || "").toLowerCase();
  if (s.includes("delay")) return "status-delayed";
  if (s.includes("board")) return "status-boarding";
  if (s.includes("land") || s.includes("arrived") || s.includes("departed")) return "status-landed";
  if (s.includes("expected") || s.includes("checkin") || s.includes("check-in") || s.includes("gate")) return "status-ontime";
  return "";
}

/* ---------- Toasts, voice announcements and the watch list ----------------
   Watch rules live on the SERVER (evaluated in its poll loop, pushed to
   ntfy/webhook independently of any browser); this panel just manages them
   over /api/watches. Matches ride the overhead snapshots as watch_events and
   surface here as toasts - and speech, if voice announcements are on. */
const toastsEl = document.getElementById("toasts");

function showToast(title, msg, cls = "") {
  const el = document.createElement("div");
  el.className = `toast ${cls}`;
  el.innerHTML = `<div class="t-title">${esc(title)}</div>` +
    (msg ? `<div class="t-msg">${esc(msg)}</div>` : "");
  toastsEl.appendChild(el);
  while (toastsEl.children.length > 4) toastsEl.firstChild.remove();
  setTimeout(() => el.remove(), 12000);
}

/* Voice: Web Speech synthesis, entirely client-side and off by default.
   Persisted per browser - a kiosk tablet keeps its setting. */
let voiceOn = localStorage.getItem("voiceOn") === "1";
const voiceBtn = document.getElementById("voice-btn");

function updateVoiceBtn() {
  voiceBtn.textContent = voiceOn ? "\u{1F50A}" : "\u{1F507}";
  voiceBtn.classList.toggle("on", voiceOn);
}
updateVoiceBtn();

voiceBtn.onclick = () => {
  voiceOn = !voiceOn;
  localStorage.setItem("voiceOn", voiceOn ? "1" : "0");
  updateVoiceBtn();
  if (voiceOn) speak("Voice announcements on.");
  else if ("speechSynthesis" in window) speechSynthesis.cancel();
};

function speak(text) {
  if (!voiceOn || !("speechSynthesis" in window) || !text) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-AU";
  speechSynthesis.speak(u);
}

function voiceFlyover(a) {
  const airline = a.airline?.airline;
  const cs = a.callsign || a.registration || "unknown aircraft";
  const model = a.info
    ? [a.info.manufacturer, a.info.model].filter(Boolean).join(" ")
    : (a.description || a.type || "");
  const route = a.route?.origin_name && a.route?.destination_name
    ? ` from ${a.route.origin_name} to ${a.route.destination_name}` : "";
  const alt = a.altitude_ft != null
    ? `, ${(Math.round(a.altitude_ft / 100) * 100).toLocaleString()} feet` : "";
  return `${airline ? airline + " " : ""}${cs} overhead${model ? ", " + model : ""}${route}${alt}.`;
}

/* Announce ring entries by diffing overhead hexes between server pushes.
   The first snapshot seeds silently - a page reload must not re-announce
   whatever is already up there. */
let prevOverheadHexes = null;
function announceOverhead(data) {
  const aircraft = data.aircraft || [];
  if (prevOverheadHexes) {
    for (const a of aircraft) {
      if (a.overhead && !prevOverheadHexes.has(a.hex)) speak(voiceFlyover(a));
    }
  }
  prevOverheadHexes = new Set(aircraft.filter((a) => a.overhead).map((a) => a.hex));
}

let prevAlertHexes = null;
function announceAlerts(data) {
  const aircraft = data.aircraft || [];
  if (prevAlertHexes) {
    for (const a of aircraft) {
      if (prevAlertHexes.has(a.hex)) continue;
      const what = a.squawk === "7600" ? "radio failure"
        : a.squawk === "7500" ? "unlawful interference" : "emergency";
      speak(`Alert. ${a.callsign || a.registration || "an aircraft"} is squawking ${what}` +
        (a.place ? ` near ${a.place}.` : "."));
    }
  }
  prevAlertHexes = new Set(aircraft.map((a) => a.hex));
}

const seenWatchEvents = new Set();
let watchEventsSeeded = false;
function processWatchEvents(events) {
  for (const ev of events) {
    if (!ev.id || seenWatchEvents.has(ev.id)) continue;
    seenWatchEvents.add(ev.id);
    if (!watchEventsSeeded) continue; // history from before this page loaded
    showToast(ev.title, ev.message, ev.kind === "squawk" ? "squawk" : "");
    speak(`${ev.title}. ${(ev.message || "").replaceAll("·", ",")}`);
  }
  watchEventsSeeded = true;
  if (seenWatchEvents.size > 500) seenWatchEvents.clear();
}

/* Watch panel: manage the server-side rules */
const watchEls = {
  btn: document.getElementById("watch-btn"),
  panel: document.getElementById("watch-panel"),
  rules: document.getElementById("watch-rules"),
  field: document.getElementById("watch-field"),
  value: document.getElementById("watch-value"),
  valueLabel: document.getElementById("watch-value-label"),
  within: document.getElementById("watch-within"),
  overhead: document.getElementById("watch-overhead"),
  golden: document.getElementById("watch-golden"),
  add: document.getElementById("watch-add"),
  note: document.getElementById("watch-note"),
};

watchEls.btn.onclick = () => {
  watchEls.panel.classList.toggle("hidden");
  watchEls.note.textContent = "";
  if (!watchEls.panel.classList.contains("hidden")) loadWatches();
};

watchEls.field.onchange = () => {
  const detector = ["circling", "squawk", "new_type"].includes(watchEls.field.value);
  watchEls.value.classList.toggle("hidden", detector);
  watchEls.valueLabel.classList.toggle("hidden", detector);
};

function watchRuleLabel(r) {
  const names = { circling: "circling aircraft", squawk: "any emergency squawk",
                  new_type: "first-ever type" };
  let s = names[r.field] || `${r.field} = ${r.value}`;
  const mods = [];
  if (r.within_nm) mods.push(`≤${r.within_nm} NM`);
  if (r.overhead_only) mods.push("overhead");
  if (r.golden_only) mods.push("golden");
  return s + (mods.length ? ` (${mods.join(", ")})` : "");
}

async function loadWatches() {
  try {
    const r = await fetch("/api/watches" + locQuery());
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    setHTML(watchEls.rules, j.rules.map((r2) =>
      `<div class="watch-rule"><span>${esc(watchRuleLabel(r2))}</span>` +
      `<button data-id="${esc(r2.id)}" title="Remove">✕</button></div>`).join("")
      || '<p class="watch-empty">No watches yet. Matches show here and push via ntfy/webhook if the server has them configured.</p>');
  } catch (e) {
    watchEls.note.textContent = `Could not load watches (${e.message})`;
  }
}

watchEls.rules.addEventListener("click", async (e) => {
  const id = e.target.dataset?.id;
  if (!id) return;
  await fetch(`/api/watches/${encodeURIComponent(id)}` + locQuery(), { method: "DELETE" });
  loadWatches();
});

watchEls.add.onclick = async () => {
  const body = {
    field: watchEls.field.value,
    value: watchEls.value.value.trim(),
    within_nm: watchEls.within.value ? +watchEls.within.value : null,
    overhead_only: watchEls.overhead.checked,
    golden_only: watchEls.golden.checked,
  };
  try {
    const r = await fetch("/api/watches" + locQuery(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${r.status}`);
    }
    watchEls.value.value = "";
    watchEls.within.value = "";
    watchEls.note.textContent = "";
    loadWatches();
  } catch (e) {
    watchEls.note.textContent = e.message;
  }
};

/* ---------- Spotting log overlay (footer button): /api/stats + replay ----
   Everything here is read-on-open: no polling, no cost while closed. The
   track map draws the last 24 h of thinned position samples as a heatmap of
   dots and can replay them: a time cursor sweeps the window and each
   aircraft's position is interpolated between its recorded samples. */
const statsEls = {
  btn: document.getElementById("stats-btn"),
  overlay: document.getElementById("stats-overlay"),
  close: document.getElementById("stats-close"),
  body: document.getElementById("stats-body"),
  cell: document.getElementById("stats-cell"),
};
let statsMap = null;
let statsMarkers = {};   // hex -> replay marker
let statsIndex = null;   // hex -> {pts: [[ts, lat, lon], ...], ptr}
let statsPlaying = null; // interval id while replaying
let statsLastT = 0;

statsEls.btn.onclick = () => {
  statsEls.overlay.classList.remove("hidden");
  loadStats();
};
statsEls.close.onclick = () => {
  statsEls.overlay.classList.add("hidden");
  stopReplay();
  if (statsMap) { statsMap.remove(); statsMap = null; statsMarkers = {}; }
};

async function loadStats() {
  statsEls.body.innerHTML =
    '<div class="empty-msg loading"><span class="spinner"></span>LOADING…</div>';
  const q = locQuery();
  try {
    const [r1, r2] = await Promise.all([
      fetch("/api/stats" + q),
      fetch("/api/history/tracks" + (q ? q + "&" : "?") + "hours=24"),
    ]);
    if (!r1.ok) throw new Error(`HTTP ${r1.status}`);
    const stats = await r1.json();
    const tracks = r2.ok ? await r2.json() : { points: [] };
    renderStats(stats, tracks);
  } catch (e) {
    statsEls.body.innerHTML =
      `<div class="empty-msg">NO SPOTTING LOG (${esc(e.message)})</div>`;
  }
}

function liRow(k, sub, v) {
  return `<li><span class="k">${k}${sub ? ` <span class="sub">${sub}</span>` : ""}</span><span class="v">${v}</span></li>`;
}

const EMPTY_LI = '<li><span class="k sub">nothing yet</span></li>';

function fmtDay(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString() : "–";
}

function hourlySVG(hourly) {
  const max = Math.max(...hourly, 1);
  const bars = hourly.map((c, h) => {
    const bh = (c / max) * 22;
    return `<rect class="bar${c === max && c > 0 ? " max" : ""}" x="${(h * 4.15 + 0.3).toFixed(2)}" y="${(25 - bh).toFixed(2)}" width="3.4" height="${bh.toFixed(2)}"></rect>` +
      (h % 3 === 0 ? `<text x="${(h * 4.15 + 2).toFixed(2)}" y="29" text-anchor="middle">${String(h).padStart(2, "0")}</text>` : "");
  }).join("");
  return `<svg viewBox="0 0 100 30">${bars}</svg>`;
}

function renderStats(s, tracks) {
  const t = s.today, a = s.alltime;
  statsEls.cell.textContent = a.since ? `TRACKING SINCE ${fmtDay(a.since)}` : "";
  const tiles = [
    ["FLYOVERS TODAY", t.flyovers, ""],
    ["AIRCRAFT TODAY", t.unique_aircraft, ""],
    ["BUSIEST HOUR", t.busiest_hour ?? "–", ""],
    ["FLYOVERS EVER", a.flyovers, "alltime"],
    ["AIRFRAMES", a.unique_aircraft, "alltime"],
    ["TYPES", a.unique_types, "alltime"],
    ["AIRLINES", a.unique_airlines, "alltime"],
  ].map(([l, v, cls]) =>
    `<div class="stats-tile ${cls}"><label>${l}</label><span>${esc(v ?? "–")}</span></div>`).join("");

  const cols = [
    ["TOP TYPES TODAY", t.top_types.map((r) =>
      liRow(esc(r.type), esc(r.description || ""), r.c))],
    ["TOP AIRLINES TODAY", t.top_airlines.map((r) =>
      liRow(esc(r.airline), esc(r.airline_iata || ""), r.c))],
    ["TOP ROUTES TODAY", t.top_routes.map((r) =>
      liRow(`${esc(r.origin)} → ${esc(r.destination)}`, "", r.c))],
    ["TOP TYPES ALL-TIME", a.top_types.map((r) =>
      liRow(esc(r.type), esc(r.description || ""), r.c))],
    ["RAREST TYPES", a.rarest_types.map((r) =>
      liRow(esc(r.type), esc(r.description || ""), `${r.c}×`))],
    ["NEWEST TYPES", a.recent_first_types.map((r) =>
      liRow(esc(r.type), esc(r.description || ""), fmtDay(r.f)))],
  ].map(([hdr, lis]) =>
    `<div><div class="stats-section-hdr">${hdr}</div><ul class="stats-list">${lis.join("") || EMPTY_LI}</ul></div>`).join("");

  const nPts = (tracks.points || []).length;
  statsEls.body.innerHTML = `
    <div class="stats-tiles">${tiles}</div>
    <div class="stats-section-hdr">FLYOVERS BY HOUR (TODAY)</div>
    <div id="stats-hourly">${hourlySVG(t.hourly)}</div>
    <div class="stats-cols">${cols}</div>
    <div class="stats-section-hdr">SKY TRACKS – LAST 24 H (${nPts} SAMPLES${tracks.truncated ? ", TRUNCATED" : ""})</div>
    <div id="stats-map"></div>
    <div class="stats-map-bar">
      <button id="replay-btn" type="button">▶ REPLAY</button>
      <input id="replay-slider" type="range">
      <span id="replay-time">NOW</span>
    </div>
    <div class="stats-note">Dots: amber below 10,000 ft, green above. Drag the slider or press replay to sweep the day.</div>`;
  initStatsMap(tracks);
}

function initStatsMap(tracks) {
  const el = document.getElementById("stats-map");
  if (!el || typeof L === "undefined") return;
  if (statsMap) { statsMap.remove(); statsMarkers = {}; }
  statsMap = L.map(el, { attributionControl: false });
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
              { maxZoom: 12 }).addTo(statsMap);
  const pts = tracks.points || [];
  if (!pts.length) { statsMap.setView([-27.4, 153.1], 8); return; }
  // Heatmap layer: cap the dot count so old tablets keep up
  const step = Math.max(1, Math.ceil(pts.length / 8000));
  const canvas = L.canvas({ padding: 0.3 });
  const bounds = [];
  for (let i = 0; i < pts.length; i += step) {
    const [, , lat, lon, alt] = pts[i];
    bounds.push([lat, lon]);
    L.circleMarker([lat, lon], {
      renderer: canvas, radius: 1.4, stroke: false,
      fillColor: alt != null && alt < 10000 ? "#ffb400" : "#3ddc84",
      fillOpacity: 0.3,
    }).addTo(statsMap);
  }
  statsMap.fitBounds(L.latLngBounds(bounds).pad(0.05));
  // Replay index: per-aircraft sample lists in time order
  statsIndex = {};
  for (const [ts, hex, lat, lon] of pts) {
    (statsIndex[hex] ??= { pts: [], ptr: 0 }).pts.push([ts, lat, lon]);
  }
  setupReplay(tracks);
}

function setupReplay(tracks) {
  const btn = document.getElementById("replay-btn");
  const slider = document.getElementById("replay-slider");
  const t0 = tracks.since, t1 = Math.floor(Date.now() / 1000);
  slider.min = t0; slider.max = t1; slider.value = t1;
  slider.oninput = () => { pauseReplay(); drawReplay(+slider.value); };
  btn.onclick = () => {
    if (statsPlaying) { pauseReplay(); return; }
    btn.textContent = "⏸ PAUSE";
    if (+slider.value >= t1 - 120) slider.value = t0; // replay from the start
    const stepS = (t1 - t0) / 1200; // whole window sweeps in ~2 minutes
    statsPlaying = setInterval(() => {
      const next = +slider.value + stepS;
      if (next >= t1) { pauseReplay(); slider.value = t1; drawReplay(t1); return; }
      slider.value = next;
      drawReplay(next);
    }, 100);
  };
}

function pauseReplay() {
  if (statsPlaying) clearInterval(statsPlaying);
  statsPlaying = null;
  const btn = document.getElementById("replay-btn");
  if (btn) btn.textContent = "▶ REPLAY";
}

function stopReplay() {
  pauseReplay();
  statsIndex = null;
}

function drawReplay(T) {
  if (!statsIndex || !statsMap) return;
  const label = document.getElementById("replay-time");
  if (label) {
    label.textContent = T >= Date.now() / 1000 - 120 ? "NOW"
      : new Date(T * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  if (T < statsLastT) for (const h in statsIndex) statsIndex[h].ptr = 0;
  statsLastT = T;
  for (const hex in statsIndex) {
    const trk = statsIndex[hex];
    while (trk.ptr < trk.pts.length - 1 && trk.pts[trk.ptr + 1][0] <= T) trk.ptr++;
    const p = trk.pts[trk.ptr], n = trk.pts[trk.ptr + 1];
    let pos = null;
    if (n && p[0] <= T && T <= n[0] && n[0] - p[0] <= 300) {
      const f = (T - p[0]) / Math.max(1, n[0] - p[0]);
      pos = [p[1] + (n[1] - p[1]) * f, p[2] + (n[2] - p[2]) * f];
    } else if (Math.abs(p[0] - T) <= 60) {
      pos = [p[1], p[2]];
    }
    if (pos) {
      if (!statsMarkers[hex]) {
        statsMarkers[hex] = L.circleMarker(pos, {
          radius: 4, stroke: false, fillColor: "#3ddc84", fillOpacity: 0.95,
        }).addTo(statsMap);
      } else {
        statsMarkers[hex].setLatLng(pos);
      }
    } else if (statsMarkers[hex]) {
      statsMap.removeLayer(statsMarkers[hex]);
      delete statsMarkers[hex];
    }
  }
}

/* ---------- Airport weather strip (METAR/TAF via /api/wx) -----------------
   Fetched lazily the first time the board renders and refreshed every ten
   minutes - matching the server's own cache TTL, so the strip stays current
   without adding upstream load. */
let wxData = null;
let wxFetchedAt = 0;
let wxInFlight = false;

function ensureWx() {
  if (wxInFlight || Date.now() - wxFetchedAt < 600_000) return;
  wxInFlight = true;
  fetch("/api/wx" + locQuery())
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => { wxData = j; wxFetchedAt = Date.now(); render(); })
    .catch(() => { wxFetchedAt = Date.now(); })  // retry in 10 min, not a loop
    .finally(() => { wxInFlight = false; });
}

function wxSummary(w) {
  const wind = w.wind_dir != null && w.wind_kt != null
    ? `WIND ${w.wind_dir === "VRB" ? "VRB" : String(Math.round(w.wind_dir)).padStart(3, "0")}/${String(Math.round(w.wind_kt)).padStart(2, "0")}${w.gust_kt ? `G${Math.round(w.gust_kt)}` : ""}KT`
    : null;
  const vis = w.visibility_sm != null ? `VIS ${w.visibility_sm} SM` : null;
  const temp = w.temp_c != null
    ? `${Math.round(w.temp_c)}°C${w.dewpoint_c != null ? `/${Math.round(w.dewpoint_c)}°C` : ""}` : null;
  const qnh = w.qnh_hpa ? `QNH ${w.qnh_hpa}` : null;
  return [wind, vis, w.wx, w.clouds, temp, qnh].filter(Boolean).map(esc).join(" · ");
}

function renderWx() {
  const el = document.getElementById("board-wx");
  if (!wxData || !wxData.raw) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  setHTML(el, `
    <div class="wx-summary">${wxSummary(wxData)}</div>
    <div class="wx-raw">${esc(wxData.raw)}</div>
    ${wxData.taf ? `<div class="wx-raw wx-taf">${esc(wxData.taf)}</div>` : ""}`);
}

function renderBoard(showDepartures) {
  const rows = (showDepartures ? board.departures : board.arrivals) || [];
  els.boardDirection.textContent = showDepartures ? "DEPARTURES" : "ARRIVALS";
  els.boardAirport.textContent = board.airport
    ? `${board.airport.name.toUpperCase()} ${board.airport.icao}` : "";
  ensureWx();
  renderWx();

  const now = Date.now() - 30 * 60 * 1000; // keep recent past 30 min on the board
  const visible = rows
    .filter((r) => !r.scheduled || new Date(r.scheduled).getTime() > now)
    .slice(0, MAX_BOARD_ROWS);

  if (board.unavailable) {
    setHTML(els.boardRows,
      `<tr><td class="board-note" colspan="5">NO BOARD DATA \u2014 SET AERODATABOX_API_KEY</td></tr>`);
    return;
  }

  setHTML(els.boardRows, visible.map((r) => {
    const est = r.estimated && r.estimated !== r.scheduled
      ? `<span class="est">→ ${esc(fmtTime(r.estimated))}</span>` : "";
    const iata = (r.flight || "").slice(0, 2).toUpperCase();
    return `
      <tr>
        <td class="col-time">${esc(fmtTime(r.scheduled))}${est}</td>
        <td class="col-flight">${logoImg(iata, "row-logo")}${esc(r.flight)}</td>
        <td class="col-city">${esc((r.city || "").toUpperCase())}</td>
        <td class="col-gate">${esc(r.gate ?? "")}</td>
        <td class="col-status ${statusClass(r.status)}">${esc((r.status || "").toUpperCase())}</td>
      </tr>`;
  }).join(""));
}
