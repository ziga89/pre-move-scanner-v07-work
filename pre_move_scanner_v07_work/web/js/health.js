// Health and universe views.
import { $, ago, esc, fmtDateTime, num } from "./util.js";

const STATE_CLS = { LIVE: "ok", PARTIAL: "warn", DISCONNECTED: "bad", CIRCUIT_OPEN: "bad", IDLE: "muted" };
const WS_LABEL = { OK: "ON", NO_KEY: "NO KEY", NA: "N/A" };

// Wallet-intelligence coverage: per-state asset counts, labelled addresses per chain,
// tracked tokens (configured vs discovered), contract discovery and provider budgets.
function walletIntelPanel(w, providers) {
  if (!w) return "";
  const states = Object.entries(w.by_state || {}).map(([k, n]) =>
    `<span class="pill"><b>${n}</b><span class="ws ws-${esc(k.toLowerCase())}">${esc(WS_LABEL[k] || k)}</span></span>`).join("");
  const labelled = Object.entries(w.labelled_addresses || {}).map(([c, t]) =>
    `<tr><td>${esc(c)}</td><td class="wrap small">${Object.entries(t).map(([k, n]) => `${esc(k)} ${n}`).join(" · ")}</td></tr>`).join("");
  const toks = Object.entries(w.tokens || {}).map(([k, n]) => `${n} ${esc(k)}`).join(" · ") || "none";
  const d = w.discovery;
  const disc = d ? `${d.known} checked (${Object.entries(d.by_state || {}).map(([k, n]) => `${esc(k)} ${n}`).join(", ") || "—"}) · ${d.pending} pending · ${d.calls} calls${d.errors ? ` · <span class="warn">${d.errors} errors</span>` : ""}`
    : "off";
  const prov = Object.entries(providers || {}).map(([name, p]) =>
    `<tr><td>${esc(name)}</td><td>${p.keyed ? "keyed" : "<b class='warn'>no key</b>"} · ${p.used_today ?? 0}/${p.daily_budget ?? 0} calls today</td></tr>`).join("");
  return `<div class="panel"><h3>Wallet intelligence</h3>
    <div class="summary"><span class="ws ws-${esc(String(w.state || "off").toLowerCase())}">${esc(w.text || "")}</span> ${states}</div>
    <table class="mini">
      <tr><td>Tracked tokens</td><td>${toks}</td></tr>
      <tr><td>Contract discovery</td><td class="wrap small">${disc}${d && d.last_error ? `<div class="bad small">${esc(d.last_error)}</div>` : ""}</td></tr>
      <tr><td>Supported chains</td><td class="wrap small">${(w.supported_chains || []).map(esc).join(", ") || "—"}</td></tr>
      <tr><td>Last poll</td><td>${w.last_poll ? `${ago(w.last_poll)} ago` : "—"}</td></tr>
      ${prov}
    </table>
    ${labelled ? `<h4>Reliable labelled addresses</h4><table class="mini">${labelled}</table>` : `<div class="muted small">no labelled addresses loaded — scores stay N/A without independent labels (unknown wallets are never given an identity)</div>`}
  </div>`;
}

export function renderHealth(h) {
  if (!h) return;
  const st = h.status || {};
  const ex = Object.values((h.engine || {}).exchanges || {});
  const exRows = ex.map(e => `<tr>
    <td><b>${esc(e.exchange)}</b></td><td class="${STATE_CLS[e.state] || ""}">${esc(e.state)}</td>
    <td>${esc(e.caps.book_mode)} / ${esc(e.caps.trade_mode)}</td><td class="num">${e.streaming}/${e.markets}</td>
    <td class="num">${e.partitions.length} × ≤${e.caps.max_symbols_per_connection}</td><td class="num">${num(e.msgs_per_s, 1)}</td>
    <td class="num">${e.reconnects}</td><td class="num">${e.errors}</td>
    <td class="wrap small">${esc((e.partitions.find(p => p.last_error) || {}).last_error || "")}
      ${(e.unavailable || []).length ? `<div class="warn">${e.unavailable.length} unavailable: ${e.unavailable.slice(0, 4).map(u => esc(u.symbol)).join(", ")}</div>` : ""}
      <div class="muted">${esc(e.caps.notes || "")}${e.caps.verified ? "" : " · limits unverified (run selftest)"}</div></td></tr>`).join("");
  const cats = Object.values(h.catalogs || {}).map(c => `<tr><td>${esc(c.exchange)}</td><td class="num">${c.markets}</td><td class="num">${c.tickers}</td>
    <td>${fmtDateTime(c.fetched_ts)}</td><td class="bad small">${esc(c.error || "")}</td></tr>`).join("");
  const vs = Object.entries(h.venue_states || {}).map(([k, v]) => `<span class="pill"><b>${v}</b>${esc(k)}</span>`).join("");
  const cg = h.coingecko || {};
  const es = h.etherscan || {};
  const sto = h.storage || {};
  const workers = ((h.engine || {}).workers || []).map(w => `<li>worker ${w.idx}: ${w.alive ? "alive" : "<b class='bad'>dead</b>"}, last seen ${ago(w.last_seen)} ago, dropped frames ${w.dropped_frames}</li>`).join("");
  $("#health").innerHTML = `
    <div class="hgrid">
      <div class="panel"><h3>Scanner</h3><table class="mini">
        <tr><td>Version / mode</td><td>${esc(h.version)} · ${esc(h.mode)}</td></tr>
        <tr><td>Engine</td><td>${esc((h.engine || {}).mode || "")} · ${(h.engine || {}).markets ?? 0} markets</td></tr>
        <tr><td>Universe</td><td class="wrap">${esc(st.universe || "")}</td></tr>
        <tr><td>Discovery</td><td class="wrap">${esc(st.discovery || "")}</td></tr>
        <tr><td>Tick</td><td>${num(st.last_tick_ms, 1)} ms (avg ${num(st.avg_tick_ms, 1)} ms) · ${st.ticks ?? 0} ticks</td></tr>
        <tr><td>Uptime</td><td>${ago(st.started)}</td></tr>
        ${st.tick_error ? `<tr><td>Tick error</td><td class="bad wrap">${esc(st.tick_error)}</td></tr>` : ""}
        ${(st.warnings || []).map(w => `<tr><td>Config</td><td class="warn wrap">${esc(w)}</td></tr>`).join("")}
      </table>${workers ? `<ul class="small">${workers}</ul>` : ""}</div>
      <div class="panel"><h3>Data sources</h3><table class="mini">
        <tr><td>CoinGecko</td><td>${cg.calls ?? 0} calls · ${cg.rate_limited ?? 0} × 429 · ${cg.keyed ? "API key" : "no key"} <div class="bad small">${esc(cg.last_error || "")}</div></td></tr>
        <tr><td>Etherscan</td><td>${es.enabled === false ? "disabled" : `${es.used_today ?? 0}/${es.daily_budget ?? 0} calls today · ${es.keyed ? "keyed" : "<b class='warn'>no key</b>"}`}
          <div class="bad small">${esc(es.last_error || "")}</div>${Object.entries(es.chain_errors || {}).map(([c, e]) => `<div class="warn small">${esc(c)}: ${esc(e)}</div>`).join("")}</td></tr>
        <tr><td>Wallet intel</td><td>${esc(h.intel_status || "")} · ${(h.labels || {}).count ?? 0} labels${((h.labels || {}).errors || []).length ? ` · <span class="warn">${h.labels.errors.length} label errors</span>` : ""}</td></tr>
        <tr><td>Signal Radar</td><td>${esc(((h.alerts || {}).radar_state) || "NONE")} · ${(h.alerts || {}).active ?? 0} active high-conviction</td></tr>
        <tr><td>Storage</td><td class="wrap">${esc(sto.path || "")}<div class="small">queue ${sto.queue ?? 0} · written ${sto.written_rows ?? 0} rows · dropped ${sto.dropped_rows ?? 0} · errors ${sto.errors ?? 0}</div>
          <div class="bad small">${esc(sto.last_error || "")}</div></td></tr>
      </table></div>
    </div>
    ${walletIntelPanel(h.wallet_intel, h.wallet_providers)}
    <div class="panel"><h3>Exchanges</h3><div class="summary">${vs}</div><div class="scroll"><table class="mini"><thead><tr>
      <th>Exchange</th><th>State</th><th>Book / trades mode</th><th class="num">Streaming</th><th class="num">Partitions</th>
      <th class="num">msg/s</th><th class="num">Reconnects</th><th class="num">Errors</th><th>Notes</th></tr></thead>
      <tbody>${exRows || "<tr><td colspan=9 class='muted'>no feeds running</td></tr>"}</tbody></table></div></div>
    <div class="hgrid">
      <div class="panel"><h3>Exchange catalogs (discovery)</h3><div class="scroll"><table class="mini"><thead><tr><th>Exchange</th><th class="num">Spot pairs</th><th class="num">Tickers</th><th>Fetched</th><th></th></tr></thead><tbody>${cats}</tbody></table></div></div>
      <div class="panel"><h3>Quote → USD</h3><div class="scroll"><table class="mini"><tbody>${(h.fx || []).map(f => `<tr><td>${esc(f.quote)}</td><td class="num">${num(f.usd, 6)}</td><td class="muted small">${esc(f.source)}</td></tr>`).join("")}</tbody></table></div></div>
    </div>`;
}

export function renderUniverse(u) {
  if (!u) return;
  const mem = (u.members || []).map(m => `<tr><td class="num">${m.rank ?? "—"}</td><td><a href="#/coin/${encodeURIComponent(m.symbol)}">${esc(m.symbol)}</a></td><td>${esc(m.name || "")}</td><td class="wrap">${(m.venues || []).map(esc).join(", ")}</td></tr>`).join("");
  const pin = (u.pinned || []).map(p => `<tr><td>${esc(String(p.symbol || "").toUpperCase())}</td><td>${esc(p.name || "")}</td><td>${esc(p.status || "")}</td></tr>`).join("");
  const exc = (u.excluded || []).map(e => `<tr><td class="num">${e.rank ?? "—"}</td><td>${esc(e.symbol)}</td><td>${esc(e.name || "")}</td><td class="wrap">${esc(e.reason)}</td></tr>`).join("");
  $("#universe").innerHTML = `
    <div class="panel"><h3>Universe: ${(u.members || []).length} of ${u.target ?? 100} target</h3>
      <p class="muted small">Market-cap ranking walked down to rank ${u.cutoff_rank ?? "—"} (scanned to ${u.deepest_rank_scanned ?? "—"}) to find
      ${u.target ?? 100} eligible coins with a usable realtime spot venue. Stablecoins, wrapped / staked / bridged tokens and tokenised gold
      are excluded. ${u.short_by ? `<b class="warn">Short by ${u.short_by}.</b>` : ""}</p>
      <div class="scroll"><table class="mini"><thead><tr><th class="num">Rank</th><th>Coin</th><th>Name</th><th>Selected venues</th></tr></thead><tbody>${mem}</tbody></table></div></div>
    <div class="hgrid">
      <div class="panel"><h3>Pinned (monitored in addition)</h3><table class="mini"><tbody>${pin || "<tr><td class='muted'>none</td></tr>"}</tbody></table></div>
      <div class="panel"><h3>Excluded / skipped</h3><div class="scroll" style="max-height:480px"><table class="mini"><thead><tr><th class="num">Rank</th><th>Coin</th><th>Name</th><th>Reason</th></tr></thead><tbody>${exc}</tbody></table></div></div>
    </div>`;
}

