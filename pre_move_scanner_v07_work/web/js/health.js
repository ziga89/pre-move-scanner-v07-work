// Health view (the Universe view lives in universe.js).
import { $, ago, esc, fmtDateTime, num } from "./util.js";

const STATE_CLS = { LIVE: "ok", PARTIAL: "warn", DISCONNECTED: "bad", CIRCUIT_OPEN: "bad", IDLE: "muted" };
const WS_LABEL = { ACTIVE: "ACTIVE", OK: "ACTIVE", NO_KEY: "NO KEY", NA: "N/A" };
const WS_ORDER = ["ACTIVE", "DISCOVERING", "WARMING", "UNSUPPORTED", "DEGRADED", "NO_KEY", "OFF", "NA"];
const PSTATE = { ok: ["ok", "OK"], off: ["muted", "OFF"], no_key: ["warn", "NO KEY"], degraded: ["bad", "DEGRADED"],
  unsupported: ["muted", "UNSUPPORTED"] };

function pstate(st, reason) {
  const [cls, label] = PSTATE[st] || ["", String(st || "—").toUpperCase()];
  return `<span class="${cls}"><b>${esc(label)}</b></span>${reason && st !== "ok" ? `<div class="muted small wrap">${esc(reason)}</div>` : ""}`;
}

// Wallet intelligence: assets per state (never a vague N/A where a reason exists), then one row per
// chain group with its provider, key status, assets covered, calls / budget, rate limits and errors.
function walletIntelPanel(w, wp) {
  if (!w) return "";
  const by = w.by_state || {};
  const states = WS_ORDER.filter(k => by[k] || ["ACTIVE", "DISCOVERING", "WARMING", "UNSUPPORTED", "DEGRADED"].includes(k))
    .map(k => `<span class="pill"><b>${by[k] || 0}</b><span class="ws ws-${esc(k.toLowerCase())}">${esc(WS_LABEL[k] || k)}</span></span>`).join("");
  const rows = ((wp || {}).chains || []).map(c => `<tr>
    <td><b>${esc(c.label)}</b><div class="muted small">${esc(c.provider_label || "no provider")}${c.shared_budget ? " · shares the Etherscan budget" : ""}</div></td>
    <td>${pstate(c.state, c.reason)}</td>
    <td class="small">${esc(c.auth || "")}</td>
    <td class="wrap small">${(c.assets || []).length ? `${c.assets.length}: ${c.assets.slice(0, 12).map(esc).join(", ")}${c.assets.length > 12 ? "…" : ""}` : "<span class='muted'>none</span>"}
      <div class="muted">${Object.entries(c.asset_states || {}).map(([k, n]) => `${esc(WS_LABEL[k] || k)} ${n}`).join(" · ")}</div></td>
    <td class="num">${c.calls ?? 0}<div class="muted small">${c.used_today ?? 0}/${c.daily_budget ?? 0} today</div></td>
    <td class="small">${c.rate_limited ? "<b class='warn'>backing off</b>" : "ok"}${c.rate_limit_hits ? ` · ${c.rate_limit_hits} hits` : ""}</td>
    <td class="num">${c.errors ?? 0}</td>
    <td class="small">${c.last_success ? ago(c.last_success) + " ago" : "—"}</td>
    <td class="wrap small bad">${esc(c.last_error || "")}${Object.entries(c.chain_errors || {}).map(([k, e]) => `<div class="warn">${esc(k)}: ${esc(e)}</div>`).join("")}</td></tr>`).join("");
  const labelled = Object.entries(w.labelled_addresses || {}).map(([c, t]) =>
    `<tr><td>${esc(c)}</td><td class="wrap small">${Object.entries(t).map(([k, n]) => `${esc(k)} ${n}`).join(" · ")}</td></tr>`).join("");
  const d = w.discovery;
  const disc = d ? `${d.known} assets known (${Object.entries(d.by_state || {}).map(([k, n]) => `${esc(k)} ${n}`).join(", ") || "—"}) · ${d.pending} pending · ${d.calls} CoinGecko calls${d.errors ? ` · <span class="warn">${d.errors} errors</span>` : ""}`
    : "off (SIM mode)";
  const ni = ((wp || {}).not_implemented || []).join(", ");
  return `<div class="panel"><h3>Wallet intelligence</h3>
    <div class="summary"><span class="ws ws-${esc(String(w.state || "off").toLowerCase())}">${esc(w.text || "")}</span> ${states}</div>
    <div class="scroll"><table class="mini"><thead><tr><th>Chain</th><th>State</th><th>Key</th><th>Assets covered</th><th class="num">Calls</th>
      <th>Rate limit</th><th class="num">Errors</th><th>Last success</th><th>Last error</th></tr></thead><tbody>${rows}</tbody></table></div>
    <table class="mini">
      <tr><td>Asset registry (chain / contract discovery)</td><td class="wrap small">${disc}${d && d.last_error ? `<div class="bad small">${esc(d.last_error)}</div>` : ""}</td></tr>
      <tr><td>Tracked assets</td><td>${Object.entries(w.tokens || {}).map(([k, n]) => `${n} ${esc(k)}`).join(" · ") || "none"}</td></tr>
      <tr><td>Last poll</td><td>${w.last_poll ? `${ago(w.last_poll)} ago` : "—"}</td></tr>
      <tr><td>No provider yet</td><td class="wrap small muted">${esc(ni)} — shown as UNSUPPORTED · provider not implemented</td></tr>
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
        <tr><td>Universe</td><td class="wrap">${esc(st.universe || "")} · ${h.manual_assets ?? 0} manual assets</td></tr>
        <tr><td>Discovery</td><td class="wrap">${esc(st.discovery || "")}</td></tr>
        <tr><td>Tick</td><td>${num(st.last_tick_ms, 1)} ms (avg ${num(st.avg_tick_ms, 1)} ms) · ${st.ticks ?? 0} ticks</td></tr>
        <tr><td>Uptime</td><td>${ago(st.started)}</td></tr>
        ${st.tick_error ? `<tr><td>Tick error</td><td class="bad wrap">${esc(st.tick_error)}</td></tr>` : ""}
        ${(st.warnings || []).map(w => `<tr><td>Config</td><td class="warn wrap">${esc(w)}</td></tr>`).join("")}
      </table>${workers ? `<ul class="small">${workers}</ul>` : ""}</div>
      <div class="panel"><h3>Data sources</h3><table class="mini">
        <tr><td>CoinGecko</td><td>${cg.calls ?? 0} calls · ${cg.rate_limited ?? 0} × 429 · ${cg.keyed ? "API key" : "no key"} <div class="bad small">${esc(cg.last_error || "")}</div></td></tr>
        <tr><td>Etherscan</td><td>${es.enabled === false ? "wallet intelligence off" : `${es.used_today ?? 0}/${es.daily_budget ?? 0} calls today · ${es.keyed ? "keyed" : "<b class='warn'>no key</b>"}`}
          <div class="bad small">${esc(es.last_error || "")}</div></td></tr>
        <tr><td>Wallet intel</td><td>${esc(h.intel_status || "")} · ${(h.labels || {}).count ?? 0} labels${((h.labels || {}).errors || []).length ? ` · <span class="warn">${h.labels.errors.length} label errors</span>` : ""}</td></tr>
        <tr><td>Signal Radar</td><td>${esc(((h.alerts || {}).radar_state) || "NONE")} · ${(h.alerts || {}).active ?? 0} active high-conviction</td></tr>
        <tr><td>Storage</td><td class="wrap">${esc(sto.path || "")}<div class="small">schema ${sto.schema_version ?? "—"}${sto.new_install ? " · new database" : sto.upgraded_from_schema != null && (sto.migrations_applied_now || []).length ? ` · upgraded from schema ${sto.upgraded_from_schema}` : ""}${sto.pre_migration_backup && sto.pre_migration_backup.backup ? ` · backup ${esc(sto.pre_migration_backup.backup)}` : ""}${sto.pre_migration_backup && sto.pre_migration_backup.skipped ? ` · <span class="warn">backup skipped: ${esc(sto.pre_migration_backup.skipped)}</span>` : ""}</div>
          <div class="small">queue ${sto.queue ?? 0} · written ${sto.written_rows ?? 0} rows · dropped ${sto.dropped_rows ?? 0} · errors ${sto.errors ?? 0}</div>
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
