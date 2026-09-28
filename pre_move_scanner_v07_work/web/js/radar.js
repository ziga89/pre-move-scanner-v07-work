// Always-visible Signal Radar bar (v0.7.3) and explicit wallet-intelligence chips.
import { $, esc, isNum, scoreCell } from "./util.js";

const STATE_CLASS = { NONE: "none", WATCH: "watch", CONFIRMING: "confirming", HIGH_CONVICTION: "hc", INVALIDATED: "invalid" };
let lastRecv = 0;
let staleTimer = null;

export function dur(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// A wallet score cell: the value when it is real, otherwise the explicit state.
export function walletCell(v, st) {
  if (st && st.state === "OK" && isNum(v)) return scoreCell(v, false);
  const state = (st && st.state) || "WARMING";
  const label = (st && st.label) || state;
  const why = (st && st.reason) || "";
  return `<span class="ws ws-${esc(state.toLowerCase())}" title="${esc(label + (why ? " — " + why : ""))}">${esc(label)}</span>`;
}

export function walletChip(w) {
  if (!w) return "";
  const state = w.state || (w.status === "unavailable" ? "NA" : "OK");
  let text = w.label || state;
  if (w.status === "supportive") text = "supportive";
  else if (w.status === "hostile") text = "hostile";
  else if (w.status === "neutral") text = "neutral";
  const cls = w.status === "supportive" ? "ok" : w.status === "hostile" ? "bad" : (state || "na").toLowerCase();
  const tip = [w.reason, (w.reshuffle_not_counted || []).length ? "reshuffling seen, not counted: " + w.reshuffle_not_counted.join(", ") : ""]
    .filter(Boolean).join(" · ");
  return `<span class="ws ws-${esc(cls)}" title="${esc(tip)}">wallet: ${esc(text)}</span>`;
}

function venuesText(e) {
  const tot = e.coverage_total ?? e.coverage ?? "—";
  const names = (e.confirmed_venues || []).join(", ");
  return `${e.confirmed ?? 0}/${tot} venues${names ? " · " + esc(names) : ""}`;
}

function entryHTML(e, now) {
  const ev = isNum(e.evidence_score) ? `${Number(e.evidence_score).toFixed(0)}/100` : "—";
  const reasons = (e.reasons || []).slice(0, 3).map(esc).join(" · ");
  let time = "";
  if (e.state === "HIGH_CONVICTION") time = `held ${dur(e.persistence_s)}${e.dipping ? " · <b class='warn'>confirmation dipping</b>" : ""}`;
  else if (e.state === "CONFIRMING") {
    const pct = Math.min(100, 100 * (e.persistence_s || 0) / (e.persistence_required_s || 120));
    time = `confirming ${dur(e.persistence_s)} / ${dur(e.persistence_required_s)} <span class="rbar"><i style="width:${pct.toFixed(0)}%"></i></span>`;
  } else if (e.state === "WATCH") time = `watching ${dur(e.persistence_s)}`;
  else if (e.state === "INVALIDATED") time = `ended ${dur(now - (e.ended_ts || now))} ago after ${dur(e.persistence_s)}`;
  const extra = e.state === "INVALIDATED"
    ? `<span class="r-why">${esc(e.end_reason || "")}${isNum(e.price_change_pct) ? ` · price ${e.price_change_pct >= 0 ? "+" : ""}${Number(e.price_change_pct).toFixed(1)}% since fire` : ""}</span>`
    : (e.state === "WATCH" || e.state === "CONFIRMING") && (e.missing || []).length
      ? `<span class="r-why">still missing: ${(e.missing || []).slice(0, 3).map(esc).join("; ")}</span>` : "";
  return `<b class="r-asset">${esc(e.asset)}</b><strong class="r-ev" title="composite evidence score — not a probability">${ev}</strong>` +
    `<span class="r-meta">${venuesText(e)} · ${time}</span>${walletChip(e.wallet)}` +
    (reasons ? `<span class="r-reasons">${reasons}</span>` : "") + extra;
}

function feedsText(f) {
  if (!f || !f.state) return "";
  if (f.state === "NO_DATA") return `<span class="bad">no live market data</span>`;
  const s = `${f.live_markets}/${f.selected_markets} markets live`;
  return f.state === "DEGRADED" ? `<span class="warn">feeds degraded · ${s}</span>` : `<span class="muted">${s}</span>`;
}

export function renderRadar(radar) {
  const el = $("#radar");
  if (!el || !radar) return;
  lastRecv = Date.now();
  el.classList.remove("stale");
  const now = Number(radar.ts) || Date.now() / 1000;
  const state = radar.state || "NONE";
  el.className = `radar radar-${STATE_CLASS[state] || "none"}`;
  el.dataset.state = state;
  const head = radar.primary;
  const w = radar.wallet || {};
  const wtxt = `<span class="r-wallet ws ws-${esc(String(w.state || "off").toLowerCase())}" title="${esc(JSON.stringify(w.by_state || {}))}">${esc(w.text || "Wallet intel —")}</span>`;
  const others = (radar.entries || []).filter(e => !head || e !== head && !(e.asset === head.asset && e.state === head.state))
    .slice(0, 4).map(e => `<button class="r-chip r-${STATE_CLASS[e.state]}" data-asset="${esc(e.asset)}">${esc(e.asset)} ${esc(e.state === "HIGH_CONVICTION" ? "HIGH-CONVICTION" : e.state)}</button>`).join("");
  const body = head
    ? `<button class="r-main" data-asset="${esc(head.asset)}">${entryHTML(head, now)}</button>`
    : `<span class="r-main r-quiet">No asset meets the mandatory gates (multi-venue structure on flat price with complete feeds). ${feedsText(radar.feeds)}</span>`;
  el.innerHTML = `<span class="r-state">${esc(radar.label || "NO HIGH-CONVICTION SETUP")}</span>${body}` +
    `<span class="r-side">${others}${wtxt}<span class="r-note">evidence ≠ probability · not proof of a purchase</span></span>`;
  el.querySelectorAll("[data-asset]").forEach(b => b.addEventListener("click", () => {
    location.hash = `#/coin/${encodeURIComponent(b.dataset.asset)}`;
  }));
  if (!staleTimer) {
    staleTimer = setInterval(() => {
      const age = (Date.now() - lastRecv) / 1000;
      const r = $("#radar");
      if (r && lastRecv && age > 15) {
        r.classList.add("stale");
        const s = r.querySelector(".r-state");
        if (s && !s.dataset.orig) s.dataset.orig = s.textContent;
        if (s) s.textContent = `${s.dataset.orig} · radar not updated for ${dur(age)}`;
      }
    }, 3000);
  }
}
