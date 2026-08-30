/* tokenDiary dashboard.
 *
 * Reads web/data.json (written by `python -m src export`) and draws everything
 * as inline SVG. No dependencies, no network: the page works over file://.
 *
 * Two constraints drive the design, both measured rather than assumed:
 *   - Cache reads are ~95% of token volume but a small share of cost, while
 *     1-hour cache writes are ~4% of volume and ~41% of cost. So "tokens" and
 *     "cost" are different questions and the metric is always explicit.
 *   - Today is provisional: a session still running can push it up, so it is
 *     drawn distinctly rather than looking like a settled day.
 */

const NS = "http://www.w3.org/2000/svg";
const SERIES = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5"];
const SEQ = ["--seq-1", "--seq-2", "--seq-3", "--seq-4", "--seq-5", "--seq-6"];

const COMPONENTS = [
  { key: "i",  label: "Input" },
  { key: "o",  label: "Output" },
  { key: "cr", label: "Cache read" },
  { key: "c5", label: "Cache write 5m" },
  { key: "c1", label: "Cache write 1h" },
];

let DATA = null;
const el = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(name).trim();

/* ---------- formatting ---------- */
const money = (v) => "$" + (v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
                                      : v.toFixed(2));
const count = (v) => v.toLocaleString();
function compact(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k";
  return String(Math.round(v));
}

/* ---------- metric ---------- */
const METRICS = {
  cost:     { label: "Notional cost", fmt: money,   of: (r) => r.cost },
  tokens:   { label: "All tokens",    fmt: compact, of: (r) => r.i + r.o + r.cr + r.c5 + r.c1 },
  billable: { label: "Tokens excl. cache read", fmt: compact, of: (r) => r.i + r.o + r.c5 + r.c1 },
  o:        { label: "Output tokens", fmt: compact, of: (r) => r.o },
  n:        { label: "API calls",     fmt: count,   of: (r) => r.n },
};

const state = { metric: "cost", groupby: "s", account: "all", sidechain: "all",
                range: "all", table: false };

/* ---------- data shaping ---------- */
function rows() {
  let rs = DATA.daily;
  if (state.account !== "all") rs = rs.filter((r) => r.s === state.account);
  if (state.sidechain === "only") rs = rs.filter((r) => r.x === 1);
  if (state.sidechain === "none") rs = rs.filter((r) => r.x === 0);

  const last = DATA.meta.coverage.last;
  if (state.range === "default" && DATA.meta.default_from) {
    rs = rs.filter((r) => r.d >= DATA.meta.default_from);
  } else if (state.range !== "all") {
    const from = shift(last, -Number(state.range) + 1);
    rs = rs.filter((r) => r.d >= from);
  }
  return rs;
}

/** The window the trend should cover: the selected range where one is chosen,
 *  otherwise first-to-last activity. */
function rangeBounds(rs) {
  if (!rs.length) return [null, null];
  const last = DATA.meta.coverage.last;
  const dates = rs.map((r) => r.d);
  let from = dates.reduce((a, b) => (a < b ? a : b));
  if (state.range !== "all" && state.range !== "default") {
    from = shift(last, -Number(state.range) + 1);
  }
  return [from, last];
}

function shift(iso, days) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Series keys in a fixed order, so a filter that removes one never repaints
 *  the survivors -- colour follows the entity, never its rank. */
function seriesKeys() {
  if (state.groupby === "component") return COMPONENTS.map((c) => c.key);
  if (state.groupby === "m") return DATA.meta.models;
  return DATA.meta.sources.map((s) => s.id);
}

function seriesLabel(key) {
  if (state.groupby === "component") return COMPONENTS.find((c) => c.key === key).label;
  if (state.groupby === "m") return key.replace(/^claude-/, "").replace(/-\d{8}$/, "");
  const s = DATA.meta.sources.find((x) => x.id === key);
  return s ? s.label : key;
}

const colorOf = (key) => css(SERIES[seriesKeys().indexOf(key) % SERIES.length]);

/** date -> { total, parts: {seriesKey: value} }, with every day in range present.
 *
 *  Days with no activity are kept as explicit zeros. Dropping them would leave
 *  gaps in a time axis that reads as uniform, which makes a quiet week look
 *  like a busy one drawn narrower. Callers that need "was there activity?"
 *  check `total > 0` -- the heatmap does, so an idle day still reads as absent
 *  rather than as a small amount.
 */
function byDay(rs) {
  const m = METRICS[state.metric];
  const out = new Map();
  const keys = seriesKeys();
  const blank = () => {
    const e = { total: 0, parts: {} };
    keys.forEach((k) => (e.parts[k] = 0));
    return e;
  };
  for (const [from, to] of [rangeBounds(rs)]) {
    if (!from) break;
    for (let d = from; d <= to; d = shift(d, 1)) out.set(d, blank());
  }
  for (const r of rs) {
    let e = out.get(r.d);
    if (!e) { e = blank(); out.set(r.d, e); }
    if (state.groupby === "component") {
      // Components only decompose an additive measure; for calls there is
      // nothing to split, so the whole bar sits in one slot.
      if (state.metric === "n") { e.parts.i += r.n; e.total += r.n; continue; }
      for (const c of COMPONENTS) {
        const v = state.metric === "cost" ? costOf(r, c.key) : (c.key in r ? r[c.key] : 0);
        if (state.metric === "billable" && c.key === "cr") continue;
        if (state.metric === "o" && c.key !== "o") continue;
        e.parts[c.key] += v; e.total += v;
      }
    } else {
      const k = state.groupby === "m" ? r.m : r.s;
      const v = m.of(r);
      if (k in e.parts) e.parts[k] += v;
      e.total += v;
    }
  }
  return out;
}

/** Apportion a row's cost across components using published rate ratios.
 *  The stored cost is authoritative for totals; this only splits it for the
 *  composition view, so it is scaled to match the row's real cost exactly. */
function costOf(r, key) {
  const W = { i: 5, o: 25, cr: 0.5, c5: 6.25, c1: 10 };   // per MTok, Opus-tier shape
  const parts = { i: r.i * W.i, o: r.o * W.o, cr: r.cr * W.cr, c5: r.c5 * W.c5, c1: r.c1 * W.c1 };
  const sum = Object.values(parts).reduce((a, b) => a + b, 0);
  return sum ? (parts[key] / sum) * r.cost : 0;
}

/* ---------- theme ---------- */
/** What the page is actually showing right now: an explicit choice if one has
 *  been made, otherwise whatever the OS asked for. */
function effectiveTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "dark" || explicit === "light") return explicit;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Label the button for what a click will DO, from what is on screen now. */
function syncThemeButton() {
  const b = el("theme");
  if (!b) return;
  const showing = effectiveTheme();
  b.textContent = showing === "dark" ? "☀" : "☾";
  b.setAttribute("aria-label", `Switch to ${showing === "dark" ? "light" : "dark"} mode`);
  b.title = b.getAttribute("aria-label");
}

/** Record an explicit choice. Only ever called from a click. */
function applyTheme(mode) {
  document.documentElement.setAttribute("data-theme", mode);
  try { localStorage.setItem("tokendiary-theme", mode); } catch (e) { /* private mode */ }
  syncThemeButton();
}

function restoreTheme() {
  let saved = null;
  try { saved = localStorage.getItem("tokendiary-theme"); } catch (e) { /* private mode */ }
  if (saved === "dark" || saved === "light") {
    applyTheme(saved);
  } else {
    // Nothing chosen yet: leave the attribute off so the page keeps following
    // the OS, and keep the label in step if the OS flips while the page is open.
    syncThemeButton();
    matchMedia("(prefers-color-scheme: dark)")
      .addEventListener("change", () => { syncThemeButton(); renderAll(); });
  }
}

/* ---------- tooltip ---------- */
const tip = () => el("tip");
function showTip(evt, html) {
  const t = tip();
  t.innerHTML = html;
  t.hidden = false;
  const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
  if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
  t.style.left = x + "px"; t.style.top = y + "px";
}
const hideTip = () => (tip().hidden = true);

function tipRows(entry, keys, fmt) {
  const items = keys.filter((k) => entry.parts[k] > 0)
    .sort((a, b) => entry.parts[b] - entry.parts[a])
    .map((k) => `<div class="row"><span><i class="dot" style="background:${colorOf(k)}"></i>${seriesLabel(k)}</span><span>${fmt(entry.parts[k])}</span></div>`);
  return items.join("");
}

/* ---------- svg helpers ---------- */
function svg(w, h, cls) {
  const s = document.createElementNS(NS, "svg");
  s.setAttribute("viewBox", `0 0 ${w} ${h}`);
  s.setAttribute("width", w); s.setAttribute("height", h);
  if (cls) s.setAttribute("class", cls);
  return s;
}
function rect(x, y, w, h, fill, r) {
  const n = document.createElementNS(NS, "rect");
  n.setAttribute("x", x); n.setAttribute("y", y);
  n.setAttribute("width", Math.max(0, w)); n.setAttribute("height", Math.max(0, h));
  n.setAttribute("fill", fill);
  if (r) { n.setAttribute("rx", r); n.setAttribute("ry", r); }
  return n;
}
function text(x, y, s, anchor) {
  const n = document.createElementNS(NS, "text");
  n.setAttribute("x", x); n.setAttribute("y", y);
  if (anchor) n.setAttribute("text-anchor", anchor);
  n.textContent = s;
  return n;
}

/* ---------- stat tiles ---------- */
function renderTiles() {
  const m = METRICS[state.metric], today = DATA.meta.today;
  const all = rows();
  const sum = (from, to) => all.filter((r) => r.d >= from && r.d <= to)
                               .reduce((a, r) => a + m.of(r), 0);
  const calls = (from, to) => all.filter((r) => r.d >= from && r.d <= to)
                                 .reduce((a, r) => a + r.n, 0);

  const weekFrom = shift(today, -6), monthFrom = today.slice(0, 8) + "01";
  const prevWeek = [shift(today, -13), shift(today, -7)];
  const first = DATA.meta.coverage.first;

  const tiles = [
    { k: "Today", v: sum(today, today), d: `${count(calls(today, today))} calls`, live: true },
    { k: "Last 7 days", v: sum(weekFrom, today), d: delta(sum(weekFrom, today), sum(prevWeek[0], prevWeek[1])) },
    { k: "This month", v: sum(monthFrom, today), d: `${count(calls(monthFrom, today))} calls` },
    { k: "All time", v: sum(first, today), d: `${DATA.meta.coverage.active_days} active days` },
  ];

  el("tiles").innerHTML = tiles.map((t) => `
    <div class="tile">
      <div class="k">${t.k}</div>
      <div class="v">${m.fmt(t.v)}</div>
      <div class="d">${t.live ? '<span class="live">live</span> · ' : ""}${t.d}</div>
    </div>`).join("");
}

function delta(now, before) {
  if (!before) return "vs prior week —";
  const pct = ((now - before) / before) * 100;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(0)}% vs prior week`;
}

/* ---------- calendar heatmap ---------- */
function renderHeatmap() {
  const m = METRICS[state.metric];
  const day = byDay(rows());
  const dates = [...day.keys()].sort();
  const host = el("heatmap");
  host.innerHTML = "";
  if (!dates.length) { host.textContent = "No data in range."; return; }

  const CELL = 12, GAP = 3, PITCH = CELL + GAP, LEFT = 30, TOP = 18;
  const start = mondayOf(dates[0]), end = dates[dates.length - 1];
  const weeks = Math.floor((dayNum(end) - dayNum(start)) / 7) + 1;
  const s = svg(LEFT + weeks * PITCH + 8, TOP + 7 * PITCH + 16);

  const max = Math.max(...[...day.values()].map((e) => e.total), 0);
  el("heat-max").textContent = `busiest day ${m.fmt(max)}`;

  ["Mon", "", "Wed", "", "Fri", "", "Sun"].forEach((lbl, i) => {
    if (lbl) s.appendChild(text(LEFT - 6, TOP + i * PITCH + CELL - 2, lbl, "end"));
  });

  let lastMonth = "";
  for (let w = 0; w < weeks; w++) {
    for (let d = 0; d < 7; d++) {
      const iso = shift(start, w * 7 + d);
      if (iso > end) continue;
      const e = day.get(iso);
      const x = LEFT + w * PITCH, y = TOP + d * PITCH;

      const mon = iso.slice(0, 7);
      if (d === 0 && mon !== lastMonth) {
        s.appendChild(text(x, TOP - 6, new Date(iso + "T00:00:00Z")
          .toLocaleString(undefined, { month: "short", timeZone: "UTC" })));
        lastMonth = mon;
      }

      // An empty day is drawn as surface with a hairline, not as the palest
      // blue: "no work" must read as absent rather than as a small amount.
      const active = e && e.total > 0;
      const cell = rect(x, y, CELL, CELL,
        active ? css(SEQ[bucket(e.total, max)]) : css("--surface-1"), 3);
      if (!active) { cell.setAttribute("stroke", css("--grid")); cell.setAttribute("stroke-width", 1); }
      if (iso === DATA.meta.today) {
        cell.setAttribute("stroke", css("--series-2"));
        cell.setAttribute("stroke-width", 1.5);
      }
      cell.style.cursor = "default";
      cell.addEventListener("mousemove", (ev) => showTip(ev,
        `<b>${iso}</b>${iso === DATA.meta.today ? " · still live" : ""}<br>` +
        (active ? `<div class="row"><span>${m.label}</span><span>${m.fmt(e.total)}</span></div>`
                + tipRows(e, seriesKeys(), m.fmt)
                : "<span>no activity</span>")));
      cell.addEventListener("mouseleave", hideTip);
      s.appendChild(cell);
    }
  }
  host.appendChild(s);

  el("heatscale").innerHTML = SEQ.map((v) =>
    `<i style="background:${css(v)}"></i>`).join("");
  el("heat-sub").textContent =
    `${m.label} per day, ${DATA.meta.tz_label}. Today is outlined — it can still rise.`;
}

const dayNum = (iso) => Math.floor(Date.parse(iso + "T00:00:00Z") / 86400000);
function mondayOf(iso) {
  const d = new Date(iso + "T00:00:00Z");
  const back = (d.getUTCDay() + 6) % 7;           // Monday-start weeks
  return shift(iso, -back);
}
function bucket(v, max) {
  if (v <= 0) return 0;
  const q = v / max;
  return q > 0.75 ? 5 : q > 0.5 ? 4 : q > 0.25 ? 3 : q > 0.08 ? 2 : 1;
}

/* ---------- stacked daily bars ---------- */
function renderTrend() {
  const m = METRICS[state.metric];
  const day = byDay(rows());
  const dates = [...day.keys()].sort();
  const host = el("trend");
  host.innerHTML = "";
  if (!dates.length) { host.textContent = "No data in range."; return; }

  const keys = seriesKeys().filter((k) => dates.some((d) => day.get(d).parts[k] > 0));
  const active = dates.filter((d) => day.get(d).total > 0).length;
  const H = 200, TOP = 12, BOT = 26, LEFT = 46;
  const BAR = dates.length > 120 ? 4 : dates.length > 60 ? 7 : 12;
  const PITCH = BAR + 3;
  const W = LEFT + dates.length * PITCH + 10;
  const max = Math.max(...dates.map((d) => day.get(d).total)) || 1;
  const s = svg(W, H);
  const scale = (v) => (v / max) * (H - TOP - BOT);

  [0, 0.5, 1].forEach((f) => {
    const y = H - BOT - f * (H - TOP - BOT);
    const g = document.createElementNS(NS, "line");
    g.setAttribute("x1", LEFT - 6); g.setAttribute("x2", W - 4);
    g.setAttribute("y1", y); g.setAttribute("y2", y);
    g.setAttribute("stroke", f === 0 ? css("--axis") : css("--grid"));
    g.setAttribute("stroke-width", 1);
    s.appendChild(g);
    s.appendChild(text(LEFT - 10, y + 3, m.fmt(max * f), "end"));
  });

  dates.forEach((iso, i) => {
    const e = day.get(iso), x = LEFT + i * PITCH;
    let y = H - BOT;
    const isToday = iso === DATA.meta.today;
    keys.forEach((k, ki) => {
      const v = e.parts[k];
      if (v <= 0) return;
      const h = scale(v);
      // 2px surface gap between stacked segments keeps adjacent fills legible.
      const r = rect(x, y - h, BAR, h - 2, colorOf(k), ki === 0 ? 0 : 0);
      if (isToday) r.setAttribute("opacity", "0.55");
      s.appendChild(r);
      y -= h;
    });
    // 4px rounded cap on the data end, anchored at the baseline.
    const capH = e.total > 0 ? Math.min(6, scale(e.total)) : 0;
    if (capH > 0) {
      const top = keys.filter((k) => e.parts[k] > 0).pop();
      const cap = rect(x, H - BOT - scale(e.total), BAR, capH, colorOf(top || keys[0]), 3);
      if (isToday) cap.setAttribute("opacity", "0.55");
      s.appendChild(cap);
    }

    const hit = rect(x - 1, TOP, BAR + 2, H - TOP - BOT, "transparent");
    hit.addEventListener("mousemove", (ev) => showTip(ev,
      `<b>${iso}</b>${isToday ? " · still live" : ""}<br>` +
      (e.total > 0
        ? `<div class="row"><span>${m.label}</span><span>${m.fmt(e.total)}</span></div>`
          + tipRows(e, keys, m.fmt)
        : "<span>no activity</span>")));
    hit.addEventListener("mouseleave", hideTip);
    s.appendChild(hit);

    if (i === 0 || i === dates.length - 1 || (dates.length > 20 && i === Math.floor(dates.length / 2))) {
      s.appendChild(text(x + BAR / 2, H - 8, iso.slice(5), "middle"));
    }
  });

  host.appendChild(s);
  el("legend").innerHTML = keys.map((k) =>
    `<span class="item"><i class="swatch" style="background:${colorOf(k)}"></i>${seriesLabel(k)}</span>`
  ).join("");
  el("trend-sub").textContent =
    `${m.label} per day by ${el("groupby").selectedOptions[0].text.toLowerCase()}. ` +
    `${dates.length} days shown, ${active} with activity — idle days are kept so the ` +
    `axis stays even. Today is drawn faded; it is not a finished day.`;
}

/* ---------- cost composition ---------- */
function renderMix() {
  const rs = rows();
  const host = el("mix");
  host.innerHTML = "";
  const tok = {}, cost = {};
  COMPONENTS.forEach((c) => { tok[c.key] = 0; cost[c.key] = 0; });
  for (const r of rs) {
    COMPONENTS.forEach((c) => { tok[c.key] += r[c.key] || 0; cost[c.key] += costOf(r, c.key); });
  }
  host.appendChild(mixBar("Token volume", tok, compact));
  host.appendChild(mixBar("Notional cost", cost, money));
}

function mixBar(title, vals, fmt) {
  const wrap = document.createElement("div");
  wrap.style.marginTop = "12px";
  const total = Object.values(vals).reduce((a, b) => a + b, 0) || 1;
  const W = 760, BAR_Y = 16, BAR_H = 20;
  const s = svg(W, 56);
  s.appendChild(text(0, 10, title));
  let x = 0;
  COMPONENTS.forEach((c, i) => {
    const w = (vals[c.key] / total) * W;
    if (w <= 0) return;
    // 2px gap between adjacent fills, same rule as the stacked bars.
    s.appendChild(rect(x, BAR_Y, Math.max(0, w - 2), BAR_H, css(SERIES[i]), 3));
    // Direct labels sit BELOW the bar in muted ink on the surface: a value
    // printed on top of a fill would be text wearing a series colour's
    // background, and three light-mode slots are already sub-3:1.
    const pct = (vals[c.key] / total) * 100;
    if (w > 40) s.appendChild(text(x + 1, BAR_Y + BAR_H + 13, `${pct.toFixed(0)}%`));
    x += w;
  });
  wrap.appendChild(s);
  const legend = document.createElement("div");
  legend.className = "legend";
  legend.innerHTML = COMPONENTS.map((c, i) =>
    `<span class="item"><i class="swatch" style="background:${css(SERIES[i])}"></i>${c.label} ${fmt(vals[c.key])}</span>`
  ).join("");
  wrap.appendChild(legend);
  return wrap;
}

/* ---------- table view (also the relief for low-contrast light slots) ---- */
function renderTable() {
  const m = METRICS[state.metric];
  const day = byDay(rows());
  const keys = seriesKeys();
  const dates = [...day.keys()].sort().reverse();
  el("table").innerHTML =
    `<thead><tr><th>Date</th>${keys.map((k) => `<th>${seriesLabel(k)}</th>`).join("")}<th>Total</th></tr></thead>` +
    `<tbody>${dates.map((d) => {
      const e = day.get(d);
      return `<tr><td>${d}${d === DATA.meta.today ? " (live)" : ""}</td>` +
        keys.map((k) => `<td>${e.parts[k] ? m.fmt(e.parts[k]) : "—"}</td>`).join("") +
        `<td><b>${m.fmt(e.total)}</b></td></tr>`;
    }).join("")}</tbody>`;
}

/* ---------- wiring ---------- */
function renderAll() {
  renderTiles(); renderHeatmap(); renderTrend(); renderMix();
  if (state.table) renderTable();
}

function boot(data) {
  DATA = data;
  const c = data.meta.coverage;
  el("coverage").textContent =
    `${count(c.calls)} API calls · ${c.first} to ${c.last} · ${c.active_days} active days · ${data.meta.tz_label}`;
  el("generated").textContent = `updated ${data.meta.generated_at.replace("T", " ").replace("Z", " UTC")}`;
  const srcs = data.meta.sources.map((s) => `${s.label} ${count(s.rows_)}`).join(" · ");
  el("footnote").innerHTML =
    `${data.meta.cost_note} Accounts: ${srcs}. Price revision ${data.meta.price_rev}. ` +
    (data.meta.pruned_files ? `${data.meta.pruned_files} source file(s) pruned by Claude — their rows are still here.` : "");

  restoreTheme();

  const acc = el("account");
  data.meta.sources.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.id; o.textContent = s.label;
    acc.appendChild(o);
  });
  ["metric", "groupby", "account", "sidechain", "range"].forEach((id) => {
    el(id).value = state[id];
    el(id).addEventListener("change", (e) => { state[id] = e.target.value; renderAll(); });
  });
  el("tableToggle").addEventListener("click", () => {
    state.table = !state.table;
    el("tableCard").hidden = !state.table;
    el("tableToggle").setAttribute("aria-expanded", String(state.table));
    if (state.table) renderTable();
  });
  el("theme").addEventListener("click", () => {
    // Flip what is CURRENTLY RENDERED, not the attribute. Cycling
    // auto -> dark -> light -> auto is broken, because "auto" renders
    // identically to whichever mode the OS is in: one step of the cycle
    // changed nothing on screen.
    applyTheme(effectiveTheme() === "dark" ? "light" : "dark");
    renderAll();                       // colours are read from CSS at draw time
  });
  renderAll();
}

// `cache: "no-store"` is load-bearing, not defensive. Python's http.server sends
// Last-Modified with no Cache-Control, and a response with no explicit freshness
// directive may be cached heuristically -- so after `export` rewrote data.json the
// page would keep rendering the previous run's numbers, with no sign anything was
// stale except the "updated ..." stamp in the header.
fetch("data.json", { cache: "no-store" })
  .then((r) => r.json())
  .then(boot)
  .catch(() => {
    el("coverage").textContent =
      "Could not load data.json. Run `python -m src export`, then serve this folder " +
      "(`python -m http.server` in web/) — some browsers block fetch over file://.";
  });
