// Coin detail view.
import { drawDepth, drawSeries, drawVolume, empty } from "./charts.js";
import { dur, walletCell } from "./radar.js";
import { $, $$, ago, badge, esc, fmtDateTime, isNum, money, na, num, price, ratioPct, scoreCell, share, signedPct } from "./util.js";

let current = null;
let hours = 6;
let histTimer = null;
let lastHistory = null;

const FAM = { thinning: "ask thinning", no_replenish: "weak replenishment", buy_flow: "aggressive buying",
  volume: "volume", bid_support: "bid support", cross_venue: "cross‑venue", onchain: "wallet context" };

export function openCoin(asset) {
  if (current !== asset) { current = asset; lastHistory = null; loadHistory(); }
  clearInterval(histTimer);
  histTimer = setInterval(loadHistory, 20000);
}
export function closeCoin() { clearInterval(histTimer); current = null; }
export function currentCoin() { return current; }

function kv(label, value, cls = "") { return `<div${cls ? ` class="${cls}"` : ""}><label>${esc(label)}</label><b>${value}</b></div>`; }

function head(d) {
  const r = d.returns || {};
  const info = d.info || {};
  const late = d.late || {};
  $("#coin-head").innerHTML = `
    <div>
      <div class="big">${esc(d.asset)} <small>${esc(d.name || "")}${info.rank ? " · #" + info.rank : ""}${d.manual ? " · <span class='mbadge'>M</span> manual asset" : ""}</small></div>
      <div style="margin-top:8px">${badge(d.status)} <span class="muted small">since ${ago(d.status_since)}</span></div>
      <div class="kv">
        ${kv("Price", price(d.price))}${kv("15m", signedPct(r[15] ?? r["15"]))}${kv("1h", signedPct(r[60] ?? r["60"]))}
        ${kv("Venues confirmed / live / selected", `${d.confirmed ?? 0} / ${d.coverage ?? 0} / ${d.coverage_total ?? 0}`)}
        ${kv("Confirming liquidity share", share(d.confirm_share))}
        ${kv("Confidence", num(d.confidence, 2))}
        ${kv("Late-move index", `${num(late.L, 2)} <span class="muted small">${esc(late.state || "")}</span>`)}
        ${kv("Price range vs normal", isNum(d.compression) ? num(d.compression, 2) + "×" : "—")}
        ${kv("Signal Radar", radarText(d), "kv-wide")}
        ${kv("Wallet intel", walletState(d.wallet_status), "kv-wide")}
      </div>
    </div>
    <div style="text-align:right">
      <div class="muted small">Pre‑Move score</div>
      <div class="big" style="font-size:54px">${isNum(d.premove) ? d.premove.toFixed(0) : "—"}</div>
      <div class="muted small">fast ${num(d.fast, 0)} · slow ${num(d.slow, 0)} · instant ${num(d.instant, 0)}</div>
      <div class="muted small">families: ${(d.families || []).map(f => esc(FAM[f] || f)).join(", ") || "none"}</div>
    </div>`;
}

function radarText(d) {
  const e = d.radar_entry;
  if (!e) return `<span class="muted">not on the radar</span>`;
  const lbl = { HIGH_CONVICTION: "HIGH-CONVICTION BUY SETUP", CONFIRMING: "CONFIRMING", WATCH: "WATCH", INVALIDATED: "INVALIDATED" }[e.state] || e.state;
  const cls = { HIGH_CONVICTION: "ok", CONFIRMING: "", WATCH: "warn", INVALIDATED: "bad" }[e.state] || "";
  const ev = isNum(e.evidence_score) ? ` · evidence ${Number(e.evidence_score).toFixed(0)}/100` : "";
  const t = e.state === "INVALIDATED" ? ` · ${esc(e.end_reason || "")}` : e.state === "HIGH_CONVICTION" ? ` · held ${dur(e.persistence_s)}` : ` · ${dur(e.persistence_s)}`;
  return `<b class="${cls}">${esc(lbl)}</b><span class="kv-note">${ev}${t}</span>`;
}

function walletState(ws) {
  if (!ws) return `<span class="ws ws-warming">WARMING</span>`;
  return `<span class="ws ws-${esc(String(ws.state).toLowerCase())}" title="${esc(ws.reason || "")}">${esc(ws.label || ws.state)}</span>
    <span class="kv-note muted">${esc(ws.reason || "")}</span>`;
}

function why(d) {
  const items = (d.reasons || []).map(t => `<li class="${String(t).startsWith("capped") ? "cap" : ""}">${esc(t)}</li>`);
  const late = d.late || {};
  let lateTxt = "";
  if (late.trigger) {
    const t = late.trigger;
    lateTxt = t.kind === "hard"
      ? `<p class="muted small">Late-move trigger: ${signedPct(t.ret)} in ${t.horizon}m vs hard threshold ${t.threshold}%.</p>`
      : `<p class="muted small">Late-move trigger: ${signedPct(t.ret)} in ${t.horizon}m = ${num(Math.abs(t.z), 1)}σ of this asset's normal ${t.horizon}m moves.</p>`;
  }
  const sig = late.sigmas || {};
  $("#coin-why").innerHTML = `<ul class="reasons">${items.join("") || "<li>No notable structure</li>"}</ul>${lateTxt}
    <p class="muted small">Normal volatility (σ): 15m ${num(sig[15] ?? sig["15"], 2)}% · 30m ${num(sig[30] ?? sig["30"], 2)}% ·
      60m ${num(sig[60] ?? sig["60"], 2)}% ${late.sigma_reliable ? "" : "(not enough history yet — hard thresholds only)"}</p>`;
}

function pipeline(d) {
  const p = d.pipeline;
  if (!p) { $("#coin-pipeline").innerHTML = "<span class='muted'>No live data.</span>"; return; }
  const caps = (p.caps || []).map(c => `<tr><td class="wrap">${esc(c.name)}: <span class="muted">${esc(c.why)}</span></td>` +
    `<td class="num">≤ ${num(c.cap, 0)}</td></tr>`).join("");
  $("#coin-pipeline").innerHTML = `<table class="mini">
    <tr><td>1 · Structural score</td><td class="num">${num(p.structural, 1)}</td></tr>
    <tr><td>2 · Compression bonus / wallet context</td><td class="num">+${num((p.compression_bonus || 0) * 100, 0)}% / ${num(p.context_points, 1)} pts → ${num(p.after_context, 1)}</td></tr>
    <tr><td>3 · Confidence (${num(d.confidence, 2)})</td><td class="num">${num(p.after_confidence, 1)}</td></tr>
    <tr><td colspan="2" class="muted small">4 · Caps (independent signals, venues, liquidity share, coverage, warm‑up)</td></tr>
    ${caps || "<tr><td class='muted'>no caps</td><td></td></tr>"}
    <tr><td>after caps</td><td class="num">${num(p.after_caps, 1)}</td></tr>
    <tr><td>5 · Late‑move multiplier (applied last)</td><td class="num">× ${num(p.late_multiplier, 2)} → ${num(p.instant, 1)}</td></tr>
    <tr><td>6 · Persistence: fast (30 s) / slow (150 s)</td><td class="num">${num(p.fast, 1)} / ${num(p.slow, 1)}</td></tr>
    <tr><td><b>Pre‑Move score</b> (current caps re‑applied)</td><td class="num"><b>${num(p.premove, 1)}</b></td></tr></table>`;
}

function subs(d) {
  const s = d.subscores || {};
  const intel = d.intel || {};
  const wsc = (d.wallet_status || {}).scores || {};
  const quiet = ["WARMING", "STALE", "NO DATA"].includes(d.status);
  const card = (label, v, dir, st) => quiet && st === undefined
    ? `<div class="sub"><label><span>${esc(label)}</span></label><div class="val muted">—</div><div class="muted small">baselines not established yet</div></div>`
    : st !== undefined && !(st && st.state === "OK" && isNum(v))
      ? `<div class="sub"><label><span>${esc(label)}</span></label><div class="val">${walletCell(null, st || { state: "WARMING", label: "WARMING" })}</div><div class="muted small">${esc((st && st.reason) || "")}</div></div>`
      : `<div class="sub"><label><span>${esc(label)}</span><span class="muted">${esc(dir || "")}</span></label>
    <div class="val">${isNum(v) ? v.toFixed(0) : na()}</div>${isNum(v) ? scoreCell(v) : ""}</div>`;
  $("#coin-subs").innerHTML = [
    card("Order-book", s.orderbook), card("Liquidity (movability)", s.liquidity), card("Buy pressure", s.buy_pressure),
    card("Cross-venue", s.cross_venue), card("MM", s.mm, intel.mm_direction, wsc.mm || null), card("Whale", s.whale, intel.whale_direction, wsc.whale || null),
    card("CEX flow", s.cex_flow, intel.cex_flow_direction, wsc.cex_flow || null),
    card("Scarcity / supply drain", s.scarcity, intel.scarcity_direction, wsc.scarcity || null),
  ].join("");
}

function venues(d) {
  const rows = (d.venues || []).map(v => {
    const conf = v.cancel_proxy_conf;
    return `<tr>
      <td><b>${esc(v.exchange)}</b><br><span class="muted small">${esc(v.symbol)}</span></td>
      <td>${badge(v.state)}<div class="muted small">${esc(v.state_reason || "")}</div></td>
      <td class="num">${money(v.discovery_volume_24h_usd)}</td>
      <td class="num">${money(v.bid_depth_1)} / ${money(v.ask_depth_1)}<div class="muted small">±0.5% ${money(v.bid_depth_05)} / ${money(v.ask_depth_05)} · ±2% ${money(v.bid_depth_2)} / ${money(v.ask_depth_2)}</div></td>
      <td class="num">${ratioPct(v.ask1_ratio)}<div class="muted small">bids ${ratioPct(v.bid1_ratio)}</div></td>
      <td class="num">${num(v.spread_bps, 1)} bps</td>
      <td class="num">${share(v.buy_share_60)}<div class="muted small">normal ${share(v.buy_share_base)}</div></td>
      <td class="num">${money(v.vol_60)}<div class="muted small">${isNum(v.vol_ratio) ? num(v.vol_ratio, 1) + "× · " : ""}${num(v.trades_60, 0)} trades</div></td>
      <td class="num">${isNum(v.ask_refill) ? num(v.ask_refill, 2) : "n/a"}<div class="muted small">gross ${isNum(v.ask_repl_60) ? num(v.ask_repl_60, 2) : "n/a"}</div></td>
      <td class="num" title="Removed ask liquidity minus aggressive buys. An estimate, not proof of cancellations.">${money(v.ask_cancel_proxy_60)}<div class="muted small">est. · ${esc(v.cancel_proxy_label || "")} (${num(conf, 2)})</div></td>
      <td class="num">${num(v.slippage_bps, 1)} bps<div class="muted small">${isNum(v.slippage_ratio) ? num(v.slippage_ratio, 2) + "× normal" : ""}</div></td>
      <td>${(v.active_families || []).map(f => esc(FAM[f] || f)).join(", ") || "<span class='muted'>—</span>"}${v.confirmed ? " <b class='up'>✓</b>" : ""}</td>
      <td class="num">${num(v.confidence, 2)}<div class="muted small">book ${num(v.book_coverage_pct, 1)}% · ${num(v.book_rate_hz, 1)}/s</div></td>
      <td><canvas class="hist" data-key="${esc(v.exchange + "|" + v.symbol)}" width="160" height="54"></canvas></td>
    </tr>`;
  }).join("");
  $("#coin-venues").innerHTML = `<div class="scroll"><table class="mini"><thead><tr>
    <th>Venue</th><th>State</th><th class="num">24h vol</th><th class="num">Depth ±1% bid / ask</th><th class="num">Asks vs normal</th>
    <th class="num">Spread</th><th class="num">Aggr. buys</th><th class="num">Volume 60s</th><th class="num">Refill after fills</th>
    <th class="num">Cancel proxy</th><th class="num">Buy slippage</th><th>Active signals</th><th class="num">Conf.</th><th>Book ±2%</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <p class="muted small">Refill after fills = (liquidity added − estimated cancels) ÷ aggressive fills; &lt; 1 means asks are not being replenished.
    Cancellation proxy = removed ask liquidity − aggressive buys, with a confidence label (L2 + trades cannot prove cancels).</p>`;
  $$("canvas.hist").forEach(c => drawDepth(c, (d.books || {})[c.dataset.key]));
}

function leadlag(d) {
  const ons = (d.onsets || []).map(o => `<tr><td>${esc(o.venue)}</td><td>${esc(FAM[o.family] || o.family)}</td><td>${ago(o.onset)} ago</td></tr>`).join("");
  const prop = d.propagation || {};
  const ll = d.leadlag || {};
  const llRows = Object.entries(ll.venues || {}).map(([v, x]) =>
    `<tr><td>${esc(v)}</td><td class="num">${x.lag_s == null ? "—" : x.lag_s + " s"}</td><td class="num">${num(x.corr, 2)}</td><td>${esc(x.note || "")}</td></tr>`).join("");
  $("#coin-leadlag").innerHTML = `
    ${prop.leader ? `<p><b>${esc(prop.leader)}</b> led ${esc(FAM[prop.family] || "")}; followers: ${(prop.followers || []).map(f => `${esc(f.venue)} (+${Math.round(f.after_s / 60)}m)`).join(", ")}</p>` : "<p class='muted'>No leader → follower propagation right now.</p>"}
    <table class="mini"><thead><tr><th>Venue</th><th>Active structural signal</th><th>Onset</th></tr></thead><tbody>${ons || "<tr><td colspan=3 class='muted'>none</td></tr>"}</tbody></table>
    <h3 style="margin-top:12px">Price lead / lag (last 10 min, 1 s mids) ${ll.reference ? `<span class="muted small">vs ${esc(ll.reference)}</span>` : ""}</h3>
    <table class="mini"><thead><tr><th>Venue</th><th class="num">Lag</th><th class="num">Corr.</th><th></th></tr></thead><tbody>${llRows || "<tr><td colspan=4 class='muted'>needs ≥ 2 live venues with price changes</td></tr>"}</tbody></table>`;
}

const SCORE_NAMES = { mm: "Market maker", whale: "Whale / treasury", cex_flow: "CEX flow", scarcity: "Scarcity" };

function registryLine(reg, ws) {
  const r = reg || {};
  const chain = r.chain_name || (ws && ws.chain_name) || r.native_chain;
  if (!chain && !r.state) return "";
  const kind = r.native_asset ? "native coin" : r.contract_address ? "token" : "";
  const src = r.source === "override" ? "manual override (verified)" : r.verified ? "on-chain verified" :
    r.contract_address ? "discovered from CoinGecko; on-chain symbol check pending" : r.native_asset ? "curated native coin" : "";
  return `<p class="small"><b>${esc(chain || "—")}</b>${kind ? " · " + kind : ""}${r.contract_address ? ` · <code>${esc(r.contract_address)}</code>` : ""}
    ${r.wallet_provider ? ` · provider ${esc(r.wallet_provider)}` : ""}${src ? ` · <span class="muted">${esc(src)}</span>` : ""}
    ${r.state && r.state !== "READY" ? `<br><span class="muted">${esc(r.state)}: ${esc(r.reason || "")}</span>` : ""}</p>`;
}

function walletStatusBlock(ws, intel, reg) {
  if (!ws) return "";
  const sc = ws.scores || {};
  const rows = Object.keys(SCORE_NAMES).map(k => `<tr><td>${SCORE_NAMES[k]}</td><td>${walletCell(intel ? intel[k] : null, sc[k])}</td>
    <td>${esc((sc[k] || {}).direction || "")}</td><td class="wrap muted small">${esc((sc[k] || {}).reason || "")}</td></tr>`).join("");
  const cands = (ws.whale_candidates || []).map(c => `<tr><td><code>${esc((c.address || "").slice(0, 12))}…</code></td><td>${esc(c.side)} ${esc(c.counterparty || "")}</td>
    <td class="num">${money(c.usd)}</td><td>${fmtDateTime(c.ts)}</td></tr>`).join("");
  const share = isNum(ws.cex_outflow_attributed_share) ? ` · exchange outflow attributed to labelled holders: ${(100 * ws.cex_outflow_attributed_share).toFixed(0)}%` : "";
  return `<p>${walletState(ws)}</p>${registryLine(reg, ws)}
    <table class="mini"><thead><tr><th>Score</th><th>State / value</th><th>Direction</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="muted small">States: OFF · NO KEY · DISCOVERING (chain / contract lookup) · WARMING (first polls / history window) · ACTIVE ·
      UNSUPPORTED (with the reason, e.g. provider not implemented) · DEGRADED (provider rate-limited / failing / out of budget) ·
      N/A (no reliable attribution) — a real 0 is shown as 0, never as N/A.${share}</p>
    ${cands ? `<h3 style="margin-top:10px">UNKNOWN / WHALE CANDIDATE <span class="muted small">unlabelled counterparties of large exchange transfers — no identity inferred, not counted in any score</span></h3>
      <table class="mini"><thead><tr><th>Address</th><th>Transfer</th><th class="num">USD</th><th>Time</th></tr></thead><tbody>${cands}</tbody></table>` : ""}`;
}

function wallet(d) {
  const w = d.wallet;
  const status = walletStatusBlock(d.wallet_status, d.intel, d.registry);
  if (!w) {
    $("#coin-wallet").innerHTML = status + `<p class="muted small">To enable: set <code>intel.enabled</code> in config.json. Bitcoin, XRPL,
      TRON, Solana, Hedera and Cardano providers need no key; EVM chains and XDC need <code>ETHERSCAN_API_KEY</code>. Scores need trusted
      labels in <code>labels/wallet_labels.csv</code>.</p>`;
    return;
  }
  const cov = w.coverage || {};
  const intel = d.intel || {};
  const bal = (w.balances || []).map(b => `<tr><td>${esc(b.entity)} <span class="muted small">${esc(b.entity_type || "")}</span></td>
    <td class="num">${num(b.balance, 0)}</td>${["1h", "6h", "24h", "7d"].map(k => `<td class="num">${b.delta[k] == null ? "—" : num(b.delta[k], 0)}</td>`).join("")}</tr>`).join("");
  const tx = (w.recent || []).slice(0, 25).map(t => `<tr><td>${fmtDateTime(t.ts)}</td><td><span class="badge">${esc(t.event_type || t.classification)}</span>
    <div class="muted small">${esc(t.attribution_confidence ? "attribution " + t.attribution_confidence : "")}</div></td>
    <td class="num">${num(t.amount, 0)}</td><td class="num">${money(t.usd_value)}</td>
    <td class="wrap">${esc(t.from_entity || (t.from_addr || "").slice(0, 10) + "…")} → ${esc(t.to_entity || (t.to_addr || "").slice(0, 10) + "…")}
    <div class="muted small">${esc(t.explanation || "")}</div></td></tr>`).join("");
  $("#coin-wallet").innerHTML = status + `
    <p class="small">${cov.covered ? `<span class="ok">Covered</span> · ${cov.polled}/${cov.addresses} labelled addresses polled${(cov.lagging || []).length ? ` · <span class="warn">${cov.lagging.length} lagging</span>` : ""} · token‑wide: ${esc(cov.token_wide || "off")}${cov.provider ? " · " + esc(cov.provider) : ""}`
      : `<span class="warn">Not covered:</span> ${esc(cov.reason || "")}`}${cov.note ? `<br><span class="muted">${esc(cov.note)}</span>` : ""}</p>
    ${(intel.reasons || []).length ? `<ul class="reasons">${intel.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    <div class="scroll"><table class="mini"><thead><tr><th>Entity</th><th class="num">Balance</th><th class="num">Δ1h</th><th class="num">Δ6h</th><th class="num">Δ24h</th><th class="num">Δ7d</th></tr></thead>
    <tbody>${bal || "<tr><td colspan=6 class='muted'>no balance snapshots yet</td></tr>"}</tbody></table></div>
    <div class="scroll" style="margin-top:10px"><table class="mini"><thead><tr><th>Time</th><th>Event</th><th class="num">Amount</th><th class="num">USD</th><th>Route</th></tr></thead>
    <tbody>${tx || "<tr><td colspan=5 class='muted'>no labelled transfers in memory</td></tr>"}</tbody></table></div>
    <p class="muted small">Events are conservative: a CEX withdrawal (CEX_OUT) is not a purchase; internal exchange moves, custody shifts
      (e.g. Coinbase Hot → Prime) and market-maker routing are never counted as buying.</p>`;
}

function timeline(events) {
  const evs = (events || []).slice().sort((a, b) => b.ts - a.ts).slice(0, 250);
  $("#coin-timeline").innerHTML = evs.map(e => {
    const pre = ((e.evidence || {}).precursors || []);
    return `<div class="tl"><span class="muted">${fmtDateTime(e.ts)}</span><span class="cat cat-${esc(e.category)}">${esc(e.category)}</span>
      <span>${esc(e.message)}${e.venue ? ` <span class="muted small">· ${esc(e.venue)}</span>` : ""}</span>
      ${pre.length ? `<div class="pre">Preceded by: ${pre.map(p => `${esc(p.message)} <b>(${p.minutes_before} min before)</b>`).join(" · ")}</div>` : ""}</div>`;
  }).join("") || "<p class='muted'>No events in this range yet.</p>";
}

function selection(d) {
  const s = d.selection;
  if (!s) { $("#coin-selection").innerHTML = "<p class='muted'>No selection yet.</p>"; return; }
  const sel = new Set((s.selected || []).map(m => m.exchange + "|" + m.symbol));
  const cand = (s.candidates || []).map(m => `<tr><td>${m.rank}</td><td>${esc(m.exchange)}</td><td>${esc(m.symbol)}</td><td class="num">${money(m.volume_24h_usd)}</td>
    <td>${sel.has(m.exchange + "|" + m.symbol) ? "<b class='up'>selected</b>" : "<span class='muted'>candidate</span>"}</td></tr>`).join("");
  const rej = (s.rejected || []).slice(0, 25).map(r => `<tr><td>${esc(r.exchange)}</td><td>${esc(r.symbol)}</td><td class="wrap">${esc(r.reason)}</td></tr>`).join("");
  const uns = (d.unsupported_top || []).map(u => `<tr><td>${esc(u.market)}</td><td>${esc(u.pair)}</td><td class="num">${money(u.volume_24h_usd)}</td><td>${esc(u.reason)}</td></tr>`).join("");
  $("#coin-selection").innerHTML = `<p class="muted small">Venues are ranked by <b>this coin's own</b> 24h spot volume on each exchange (never by global exchange size).</p>
    <div class="hgrid"><div class="scroll"><table class="mini"><thead><tr><th>#</th><th>Exchange</th><th>Pair</th><th class="num">24h volume</th><th></th></tr></thead><tbody>${cand}</tbody></table></div>
    <div class="scroll"><table class="mini"><thead><tr><th>Exchange</th><th>Pair</th><th>Rejected because</th></tr></thead><tbody>${rej || "<tr><td colspan=3 class='muted'>none</td></tr>"}</tbody></table></div></div>
    ${uns ? `<h3 style="margin-top:12px">Top markets on unsupported exchanges</h3><table class="mini"><tbody>${uns}</tbody></table>` : ""}`;
}

export function renderCoin(d) {
  if (!d || d.asset !== current) return;
  head(d); why(d); pipeline(d); subs(d); venues(d); leadlag(d); wallet(d); selection(d);
  if (!lastHistory) timeline(d.events);
}

async function loadHistory() {
  if (!current) return;
  const asset = current;
  $("#history-subtitle").textContent = `${asset} · last ${hours === 168 ? "7 days" : hours + "h"} · refreshes every 20 s`;
  try {
    const r = await fetch(`/api/history/${encodeURIComponent(asset)}?hours=${hours}&max_points=1500`, { cache: "no-store" });
    const d = await r.json();
    if (asset !== current) return;
    lastHistory = d;
    drawHistory(d);
    timeline(d.events);
  } catch (err) {
    $("#history-subtitle").textContent = `History error: ${err}`;
  }
}

function drawHistory(d) {
  const rows = d.composite || [];
  const legacy = d.legacy || [];
  // Charts mark only the decisive events; book/venue onsets stay in the timeline.
  const MARK = new Set(["STATUS", "SCORE", "PRICE", "WALLET", "ALERT", "LEGACY"]);
  const bands = d.alerts || [];   // high-conviction alert periods from the alerts table
  const ev = (d.events || []).filter(e => MARK.has(e.category));
  const pct = v => (v * 100).toFixed(0) + "%";
  drawSeries($("#ch-price"), rows, [
    { label: "Price", get: r => r.price, color: "#5eead4" },
    { label: "v0.6 price", get: r => r.price, color: "#64748b", dash: [4, 4], rows: legacy, noLegend: !legacy.length },
    { label: "Pre‑Move", get: r => r.score, color: "#fb7185", axis: "right" },
    { label: "v0.6 score", get: r => r.score, color: "#94a3b8", dash: [4, 4], axis: "right", rows: legacy, noLegend: !legacy.length },
  ], { events: ev, bands, rmin: 0, rmax: 100, formatY: v => "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 6 }), formatR: v => Math.round(v) });
  drawSeries($("#ch-subs"), rows, [
    { label: "Pre‑Move (max)", get: r => r.score, color: "#fb7185", width: 2 },
    { label: "fast", get: r => r.fast, color: "#fbbf24", dash: [3, 3] },
    { label: "order‑book", get: r => r.orderbook, color: "#60a5fa" },
    { label: "liquidity", get: r => r.liquidity, color: "#a78bfa" },
    { label: "buy", get: r => r.buy_pressure, color: "#34d399" },
    { label: "cross‑venue", get: r => r.cross_venue, color: "#f97316" },
  ], { events: ev, bands, ymin: 0, ymax: 100, formatY: v => Math.round(v) });
  drawSeries($("#ch-flow"), rows, [
    { label: "Ask depth vs baseline", get: r => r.ask_depth_ratio, color: "#60a5fa" },
    { label: "Aggressive buy ratio", get: r => r.buy_ratio_60s, color: "#34d399" },
    { label: "Volume vs baseline (÷4)", get: r => (r.volume_ratio == null ? null : Math.min(Number(r.volume_ratio), 4) / 4), color: "#fbbf24" },
    { label: "v0.6 ask depth", get: r => r.ask_depth_ratio, color: "#64748b", dash: [4, 4], rows: legacy, noLegend: !legacy.length },
  ], { events: ev, ymin: 0, ymax: 1.3, formatY: pct });
  drawVolume($("#ch-volume"), rows.length ? rows : legacy, ev);
  drawSeries($("#ch-spread"), rows, [
    { label: "Spread (bps)", get: r => r.spread_bps, color: "#f97316" },
    { label: "Slippage × normal", get: r => r.slippage_ratio, color: "#c084fc", axis: "right" },
  ], { events: ev, formatR: v => v.toFixed(2) + "×" });
  drawSeries($("#ch-repl"), rows, [
    { label: "Refill after fills", get: r => r.refill, color: "#34d399" },
    { label: "Cancel proxy $ (est.)", get: r => r.cancel_proxy, color: "#fb7185", axis: "right" },
  ], { events: ev, formatR: v => money(v) });
  const venues = d.venues || {};
  const series = [];
  const pal = ["#5eead4", "#fbbf24", "#fb7185", "#60a5fa", "#c084fc", "#34d399", "#f97316", "#a3e635"];
  Object.entries(venues).forEach(([v, vr], i) => series.push({ label: v, rows: vr, get: r => r.ask_depth_ratio, color: pal[i % pal.length] }));
  if (series.length) drawSeries($("#ch-venues"), [], series, { events: ev, ymin: 0, formatY: pct });
  else empty($("#ch-venues"), "No venue history yet.");
  $("#venue-legend").textContent = series.length ? "100% = the venue's own normal; falling lines = sell-side liquidity thinning. The first line to fall shows which exchange moved first." : "";
}

export function initCoin() {
  $$(".range-buttons button").forEach(btn => btn.addEventListener("click", () => {
    $$(".range-buttons button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    hours = Number(btn.dataset.hours);
    loadHistory();
  }));
  window.addEventListener("resize", () => { if (current && lastHistory) drawHistory(lastHistory); });
}
