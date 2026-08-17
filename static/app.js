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

let overhead = { aircraft: [], overhead_count: 0 };
let board = { arrivals: [], departures: [] };
let alerts = { aircraft: [] }; // global squawk-7700 watch (worldwide)
let overheadLoaded = false;   // first payload received: empty now means CLEAR SKIES
let accessDenied = false;     // server 403'd us (REQUIRE_DEVICE_TOKEN without a token)
let lastTraffic = 0;          // timestamp of last non-empty radar snapshot
let lastOverhead = 0;         // timestamp of last aircraft inside the overhead ring
let spotlightHex = null;      // sticky spotlight: don't flip between overhead planes

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
      overhead = msg.data;
      overheadLoaded = true;
      if (overhead.aircraft.length > 0) lastTraffic = Date.now();
      if (overhead.overhead_count > 0) lastOverhead = Date.now();
      render();
    } else if (msg.type === "board") {
      board = msg.data;
      els.mockBadge.classList.toggle("hidden", !board.mock);
      render();
    } else if (msg.type === "alerts") {
      alerts = msg.data;
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
  let secs = toRing / (a.ground_speed_kt / 3600);
  if (overhead.updated) secs -= Math.max(0, Date.now() / 1000 - overhead.updated);
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
  setHTML(els.spotlightView.querySelector(".sp-eta"),
    eta != null ? `OVERHEAD IN ${fmtEta(eta)}` : "");
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
          <span>${alt}</span><span>${spd}</span><span>${dist}</span>${phase}${etaTag}
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

function renderBoard(showDepartures) {
  const rows = (showDepartures ? board.departures : board.arrivals) || [];
  els.boardDirection.textContent = showDepartures ? "DEPARTURES" : "ARRIVALS";
  els.boardAirport.textContent = board.airport
    ? `${board.airport.name.toUpperCase()} ${board.airport.icao}` : "";

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
