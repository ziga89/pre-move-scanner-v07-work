// "Top anomalies right now" table.
// Rows are keyed by coin and updated in place (v0.6 rebuilt all cards every
// second, which collapsed open panels and cost a full re-render at Top-100).
import { $, $$, badge, esc, isNum, scoreCell, signedPct } from "./util.js";

const PRE = new Set(["WATCH", "EMERGING", "CONFIRMED PRE-MOVE", "STRONG PRE-MOVE"]);
const LATE = new Set(["LATE", "MOVE IN PROGRESS"]);
const QUIET = new Set(["WARMING", "STALE", "NO DATA"]);
const rowsByAsset = new Map();
let last = null;
let sortKey = null;
let sortDir = -1;


function ageText(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  return r ? `${m}m ${r}s` : `${m}m`;
}

function renderAlerts(alerts) {
  const rail = $("#alert-rail");
  if (!rail) return;
  const rows = Array.isArray(alerts) ? alerts : [];
  if (!rows.length) {
    rail.hidden = true;
    rail.innerHTML = "";
    return;
  }
  rail.hidden = false;
  rail.innerHTML = rows.map(a => {
    const reasons = (a.reasons || []).slice(0, 4).map(esc).join(" · ");
    const venues = (a.confirmed_venues || []).map(esc).join(", ");
    const wallet = a.wallet_status === "supportive" ? "wallet/CEX supportive" :
      a.wallet_status === "unavailable" ? "wallet N/A — strict market-only threshold" :
      a.wallet_status === "neutral" ? "wallet neutral" : esc(a.wallet_status || "wallet N/A");
    return `<button class="hc-alert" data-asset="${esc(a.asset)}" title="Evidence score is not a probability or proof of a purchase.">` +
      `<span class="hc-kicker">HIGH-CONVICTION BUY SETUP</span>` +
      `<span class="hc-main"><b>${esc(a.asset)}</b><strong>${Number(a.evidence_score || 0).toFixed(0)}/100</strong>` +
      `<span>${a.confirmed ?? 0}/${a.coverage ?? 0} venues · ${ageText(a.duration_s)} · ${esc(wallet)}</span></span>` +
      `<span class="hc-reason">${reasons}${venues ? ` · [${venues}]` : ""}</span>` +
      `<span class="hc-note">composite evidence — not proof of a purchase</span></button>`;
  }).join("");
  rail.querySelectorAll(".hc-alert").forEach(b => b.addEventListener("click", () => {
    location.hash = `#/coin/${encodeURIComponent(b.dataset.asset)}`;
  }));
}

function subCell(v, quiet) {
  // During warm-up / without live data a structural 0 would read as "measured, nothing found".
  if (quiet) return `<span class="muted" title="not available until baselines are established">—</span>`;
  return scoreCell(v, false);
}

function venuesCell(r) {
  const tot = r.coverage_total ?? "—";
  const live = r.coverage ?? 0;
  const conf = r.confirmed ?? 0;
  const cls = conf >= 3 ? "up" : conf >= 2 ? "warn" : "muted";
  return `<span class="${cls}" title="confirmed / live / selected venues">${conf}</span><span class="muted">/${live}/${tot}</span>`;
}

function retCell(v) {
  if (!isNum(v)) return `<span class="muted">—</span>`;
  const cls = v >= 3 ? "down" : v <= -3 ? "down" : Math.abs(v) < 1 ? "muted" : "";
  return `<span class="${cls}">${signedPct(v)}</span>`;
}

function buildRow(r) {
  const tr = document.createElement("tr");
  tr.dataset.asset = r.asset;
  tr.innerHTML = "<td class='pos'></td><td class='coin'></td><td class='num pm'></td><td class='num liq'></td>" +
    "<td class='num ob'></td><td class='num bp'></td><td class='num cv'></td><td class='num mm'></td>" +
    "<td class='num wh'></td><td class='num cx'></td><td class='num r15'></td><td class='num r60'></td>" +
    "<td class='num ven'></td><td class='reason'></td><td class='st'></td>";
  tr.addEventListener("click", () => { location.hash = `#/coin/${encodeURIComponent(r.asset)}`; });
  return tr;
}

function setCell(tr, cls, html) {
  const td = tr.querySelector("td." + cls);
  if (td && td._h !== html) { td.innerHTML = html; td._h = html; }
}

function passes(r) {
  const q = $("#f-search").value.trim().toUpperCase();
  if (q && !(r.asset.includes(q) || String(r.name || "").toUpperCase().includes(q))) return false;
  const st = $("#f-status").value;
  if (st === "premove" && !PRE.has(r.status)) return false;
  if (st === "LATE" && !LATE.has(r.status)) return false;
  if (st && st !== "premove" && st !== "LATE" && r.status !== st) return false;
  if ($("#f-hide-late").checked && LATE.has(r.status) && st !== "LATE") return false;
  if ($("#f-pinned").checked && !r.pinned) return false;
  return true;
}

export function renderTop(data) {
  if (!data || !data.rows) return;
  last = data;
  renderAlerts(data.alerts);
  const tbody = $("#top-table tbody");
  const seen = new Set();
  let rows = data.rows.slice();
  if (sortKey) {
    rows.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * sortDir;
    });
  }
  for (const r of rows) {
    seen.add(r.asset);
    let tr = rowsByAsset.get(r.asset);
    if (!tr) { tr = buildRow(r); rowsByAsset.set(r.asset, tr); }
    setCell(tr, "pos", String(r.position));
    setCell(tr, "coin", `<b>${esc(r.asset)}</b>${r.pinned ? "<span class='pin' title='pinned (in addition to the Top 100)'>★</span>" : ""}<small>${esc(r.name || "")}${r.rank ? " · #" + r.rank : ""}</small>`);
    const quiet = QUIET.has(r.status);
    setCell(tr, "pm", quiet ? subCell(null, true) : `<span title="fast ${r.fast ?? "—"} · slow ${r.slow ?? "—"}">${scoreCell(r.premove)}</span>`);
    setCell(tr, "liq", subCell(r.liquidity, quiet));
    setCell(tr, "ob", subCell(r.orderbook, quiet));
    setCell(tr, "bp", subCell(r.buy_pressure, quiet));
    setCell(tr, "cv", subCell(r.cross_venue, quiet));
    setCell(tr, "mm", subCell(r.mm));
    setCell(tr, "wh", subCell(r.whale));
    setCell(tr, "cx", subCell(r.cex_flow));
    setCell(tr, "r15", retCell(r.r15));
    setCell(tr, "r60", retCell(r.r60));
    setCell(tr, "ven", venuesCell(r));
    setCell(tr, "reason", esc(r.reason || "") + (r.cap_reason ? `<div class="muted small">${esc(r.cap_reason)}</div>` : ""));
    setCell(tr, "st", badge(r.status));
    tr.classList.toggle("hidden", !passes(r));
    tbody.appendChild(tr);  // re-appending moves the row into ranked order
  }
  for (const [a, tr] of rowsByAsset) {
    if (!seen.has(a)) { tr.remove(); rowsByAsset.delete(a); }
  }
  const c = data.counts || {};
  const order = ["STRONG PRE-MOVE", "CONFIRMED PRE-MOVE", "EMERGING", "WATCH", "NORMAL", "LOW CONFIDENCE",
    "MOVE IN PROGRESS", "LATE", "WARMING", "STALE", "NO DATA"];
  $("#summary").innerHTML = order.filter(k => c[k]).map(k => `<span class="pill"><b>${c[k]}</b>${esc(k)}</span>`).join("") +
    `<span class="pill">${esc(data.universe || "")}</span>`;
}

export function initTop() {
  for (const id of ["#f-search", "#f-status", "#f-hide-late", "#f-pinned"]) {
    $(id).addEventListener("input", () => renderTop(last));
    $(id).addEventListener("change", () => renderTop(last));
  }
  $$("#top-table th[data-sort]").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (k === "position") { sortKey = null; } else if (sortKey === k) { sortDir = -sortDir; } else {
      sortKey = k; sortDir = ["asset", "status"].includes(k) ? 1 : -1;
    }
    renderTop(last);
  }));
}
