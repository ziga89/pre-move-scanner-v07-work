// Universe page: the Top-100 members, the persistent MANUAL ASSETS (search → select → preview → add /
// remove, no restart) and every excluded coin with its reason. A ticker is never resolved automatically:
// the server returns candidates and you pick the CoinGecko id.
import { $, ago, esc, money, price } from "./util.js";

let lastUniverse = null;
let busy = false;

const STATE_CLS = { "ACTIVE": "ok", "WARMING": "warn", "STALE": "warn", "NO VENUE": "bad", "BLOCKED": "bad",
  "NEEDS SELECTION": "warn", "PENDING": "muted", "NOT MONITORED": "muted" };

function walletBadge(w) {
  if (!w || !w.state) return `<span class="ws ws-discovering">—</span>`;
  return `<span class="ws ws-${esc(String(w.state).toLowerCase())}" title="${esc(w.reason || "")}">${esc(w.label || w.state)}</span>`;
}

function manualRow(m) {
  const chain = m.chain ? `${esc(m.chain)}${m.native === true ? " · native" : m.contract ? " · token" : ""}`
    : `<span class="muted">${m.registry_state === "PENDING" || m.registry_state === "ERROR" ? "resolving…" : esc(m.registry_state || "—")}</span>`;
  const cands = (m.candidates || []).length
    ? `<div class="small warn">${(m.candidates || []).map(c => `<button class="linkbtn" data-add="${esc(c.id)}">${esc(c.name || c.id)} (${esc(c.id)}${c.rank ? " · #" + c.rank : ""})</button>`).join(" ")}</div>` : "";
  return `<tr>
    <td><b>${m.monitored ? `<a href="#/coin/${encodeURIComponent(m.symbol)}">${esc(m.symbol)}</a>` : esc(m.symbol)}</b></td>
    <td>${esc(m.name || "")}<div class="muted small">${esc(m.coingecko_id || "")}${m.rank ? " · #" + m.rank : ""}${m.in_top ? " · in Top-100" : ""}</div></td>
    <td class="wrap small">${chain}${m.contract ? `<div class="muted"><code>${esc(String(m.contract).slice(0, 18))}…</code></div>` : ""}</td>
    <td><span class="mstate ${STATE_CLS[m.state] || ""}">${esc(m.state)}</span><div class="muted small wrap">${esc(m.status || "")}</div>${cands}</td>
    <td>${walletBadge(m.wallet)}</td>
    <td><button class="btn-small" data-remove="${esc(m.symbol)}">Remove</button></td></tr>`;
}

function candidateRow(c) {
  const venues = (c.venues || []).map(v => esc(v.exchange)).join(", ") || "<span class='muted'>none found</span>";
  const md = c.metadata;
  const meta = md ? `${esc(md.chain_name || md.native_chain || "")}${md.native_asset ? " · native" : md.contract_address ? " · token" : ""}` +
    `<div class="muted small">${esc(md.state || "")}${md.wallet_provider ? " · " + esc(md.wallet_provider) : ""}</div>` : "<span class='muted small'>preview for chain / contract</span>";
  const flags = c.in_top100 ? "<span class='pill'>in Top-100</span>" : c.manual ? "<span class='pill'>manual already</span>" : "";
  const taken = c.ticker_taken_by ? `<div class="bad small">ticker used by ${esc(c.ticker_taken_by)}</div>` : "";
  return `<tr>
    <td><b>${esc(c.symbol)}</b></td><td>${esc(c.name || "")}<div class="muted small">${esc(c.coingecko_id)}</div></td>
    <td class="num">${c.market_cap_rank ? "#" + c.market_cap_rank : "—"}</td><td class="num">${price(c.price_usd)}</td>
    <td class="wrap small">${meta}</td><td class="wrap small">${venues}</td>
    <td>${flags}${taken}<button class="btn-small" data-preview="${esc(c.coingecko_id)}">Preview</button></td></tr>`;
}

function previewHTML(p) {
  const c = p.candidate || {};
  const md = c.metadata || {};
  const w = p.wallet || {};
  const ps = w.provider_state || {};
  const venues = (c.venues || []).map(v => `${esc(v.exchange)} ${esc(v.symbol)} <span class="muted">${money(v.volume_24h_usd)}</span>`).join("<br>") ||
    "<span class='warn'>no usable exchange market found (it will be kept and re-checked)</span>";
  return `<div class="preview">
    <h4>${esc(c.symbol)} · ${esc(c.name || "")} <span class="muted small">${esc(c.coingecko_id)}</span></h4>
    <table class="mini">
      <tr><td>Market-cap rank</td><td>${c.market_cap_rank ? "#" + c.market_cap_rank : "—"} · ${price(c.price_usd)}</td></tr>
      <tr><td>Chain / platform</td><td>${esc(md.chain_name || md.native_chain || "—")} · ${md.native_asset ? "native coin" : md.contract_address ? "token" : "—"}</td></tr>
      <tr><td>Contract</td><td class="wrap"><code>${esc(md.contract_address || "—")}</code></td></tr>
      <tr><td>Wallet intelligence</td><td>${w.supported ? `<span class="ok">supported</span> · ${esc(w.provider || "")}` : `<span class="warn">not supported</span>`}
        <div class="muted small">${esc(md.reason || w.reason || "")}${ps.state && ps.state !== "ok" ? " · provider: " + esc(ps.state) + " (" + esc(ps.reason || "") + ")" : ""}${w.intel_enabled === false ? " · wallet intelligence is OFF in the config" : ""}</div></td></tr>
      <tr><td>Exchange venues</td><td class="small">${venues}</td></tr>
    </table>
    ${c.ticker_taken_by ? `<p class="bad small">The ticker ${esc(c.symbol)} is already monitored as ${esc(c.ticker_taken_by)}.</p>` :
      c.in_top100 ? `<p class="muted small">Already in the Top-100: adding it marks it manual (kept even if it drops out), without a duplicate.</p>` : ""}
    <button class="btn" data-add="${esc(c.coingecko_id)}">Add ${esc(c.symbol)} as manual asset</button>
  </div>`;
}

async function api(method, url, body) {
  const r = await fetch(url, { method, cache: "no-store", headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch (e) { data = { detail: String(e) }; }
  return { ok: r.ok, status: r.status, data };
}

function setMsg(html, cls = "") {
  const el = $("#manual-msg");
  if (el) { el.className = "small " + cls; el.innerHTML = html; }
}

async function search() {
  const q = $("#manual-q").value.trim();
  if (!q || busy) return;
  busy = true;
  setMsg("searching CoinGecko…", "muted");
  $("#manual-preview").innerHTML = "";
  const r = await api("GET", `/api/assets/search?q=${encodeURIComponent(q)}`);
  busy = false;
  const cands = (r.data && r.data.candidates) || [];
  if (!r.ok) { setMsg(esc((r.data && r.data.detail) || "search failed"), "bad"); return; }
  if (!cands.length) { setMsg(`No CoinGecko coin matches “${esc(q)}”.`, "warn"); $("#manual-cands").innerHTML = ""; return; }
  setMsg(r.data.ambiguous ? `<b>${r.data.exact_ticker_matches} coins use the ticker ${esc(q.toUpperCase())}</b> — select the one you mean (never guessed).`
    : "Select a coin and check the preview before adding it.", r.data.ambiguous ? "warn" : "muted");
  $("#manual-cands").innerHTML = `<div class="scroll"><table class="mini"><thead><tr><th>Ticker</th><th>Name / CoinGecko id</th><th class="num">Rank</th>
    <th class="num">Price</th><th>Chain / platform</th><th>Venues found</th><th></th></tr></thead><tbody>${cands.map(candidateRow).join("")}</tbody></table></div>`;
  if (cands.length === 1) preview(cands[0].coingecko_id);
}

async function preview(id) {
  setMsg(`loading ${esc(id)} (chain, platform, contract)…`, "muted");
  const r = await api("GET", `/api/assets/resolve/${encodeURIComponent(id)}`);
  if (!r.ok) { setMsg(esc((r.data && r.data.detail) || "preview failed"), "bad"); return; }
  setMsg("");
  $("#manual-preview").innerHTML = previewHTML(r.data);
}

async function add(id) {
  setMsg(`adding ${esc(id)}…`, "muted");
  const r = await api("POST", "/api/assets/manual", { coingecko_id: id });
  if (!r.ok) {
    setMsg(esc((r.data && r.data.detail) || `failed (${r.status})`), "bad");
    return;
  }
  const a = r.data.asset || {};
  setMsg(`<b>${esc(a.symbol || id)}</b>: ${esc(r.data.status === "already" ? "already a manual asset" : r.data.integration || "added")}`, "ok");
  $("#manual-preview").innerHTML = "";
  $("#manual-cands").innerHTML = "";
  load();
}

async function remove(sym) {
  if (!window.confirm(`Stop monitoring ${sym} as a manual asset? Its stored history is kept.`)) return;
  const r = await api("DELETE", `/api/assets/manual/${encodeURIComponent(sym)}`);
  setMsg(r.ok ? `<b>${esc(sym)}</b>: ${esc(r.data.result)}` : esc((r.data && r.data.detail) || "failed"), r.ok ? "ok" : "bad");
  load();
}

let wired = false;

function wire(root) {
  // one delegated listener: candidate / preview buttons are inserted after the page renders
  if (wired) return;
  wired = true;
  root.addEventListener("click", e => {
    const b = e.target.closest("[data-preview],[data-add],[data-remove]");
    if (!b || !root.contains(b)) return;
    e.preventDefault();
    if (b.dataset.preview) preview(b.dataset.preview);
    else if (b.dataset.add) add(b.dataset.add);
    else if (b.dataset.remove) remove(b.dataset.remove);
  });
}

export function renderUniverse(u) {
  if (!u) return;
  lastUniverse = u;
  const el = $("#universe");
  const keepQ = $("#manual-q") ? $("#manual-q").value : "";
  const keepCands = $("#manual-cands") ? $("#manual-cands").innerHTML : "";
  const keepPrev = $("#manual-preview") ? $("#manual-preview").innerHTML : "";
  const keepMsg = $("#manual-msg") ? $("#manual-msg").outerHTML : `<div id="manual-msg" class="small"></div>`;
  const mem = (u.members || []).map(m => `<tr><td class="num">${m.rank ?? "—"}</td><td><a href="#/coin/${encodeURIComponent(m.symbol)}">${esc(m.symbol)}</a>${m.manual ? " <span class='mbadge' title='manual asset'>M</span>" : ""}</td><td>${esc(m.name || "")}</td><td class="wrap">${(m.venues || []).map(esc).join(", ")}</td></tr>`).join("");
  const man = (u.manual || []).map(manualRow).join("");
  const exc = (u.excluded || []).map(e => `<tr><td class="num">${e.rank ?? "—"}</td><td>${esc(e.symbol)}</td><td>${esc(e.name || "")}</td><td class="wrap">${esc(e.reason)}</td></tr>`).join("");
  const cnt = u.counts || {};
  el.innerHTML = `
    <div class="panel" id="manual-panel"><h3>Manual assets <span class="muted small">${cnt.manual ?? 0} · ${cnt.manual_outside_top ?? 0} outside the Top-100 · monitored universe ${cnt.monitored ?? "—"} assets</span></h3>
      <form id="manual-form" class="manual-form" autocomplete="off">
        <input id="manual-q" type="search" placeholder="Search ticker, name or CoinGecko ID…" aria-label="Search ticker, name or CoinGecko ID" value="${esc(keepQ)}">
        <button class="btn" type="submit">Search</button>
      </form>
      ${keepMsg}
      <div id="manual-cands">${keepCands}</div>
      <div id="manual-preview">${keepPrev}</div>
      <div class="scroll"><table class="mini manual-table"><thead><tr><th>Ticker</th><th>Name</th><th>Chain</th><th>Status</th><th>Wallet</th><th></th></tr></thead>
        <tbody>${man || "<tr><td colspan=6 class='muted'>no manual assets — search above to add one</td></tr>"}</tbody></table></div>
      <p class="muted small">Manual assets are monitored in addition to the Top-100 and stay until you remove them. They are ranked exactly like every other
      asset (manual status never changes a score or the order). Removing one stops monitoring; its history is kept.</p>
    </div>
    <div class="panel"><h3>Universe: ${(u.members || []).length} of ${u.target ?? 100} target</h3>
      <p class="muted small">Market-cap ranking walked down to rank ${u.cutoff_rank ?? "—"} (scanned to ${u.deepest_rank_scanned ?? "—"}) to find
      ${u.target ?? 100} eligible coins with a usable realtime spot venue. Stablecoins, wrapped / staked / bridged tokens and tokenised gold
      are excluded. ${u.short_by ? `<b class="warn">Short by ${u.short_by}.</b>` : ""} ${u.ts ? `Updated ${ago(u.ts)} ago.` : ""}</p>
      <div class="scroll"><table class="mini"><thead><tr><th class="num">Rank</th><th>Coin</th><th>Name</th><th>Selected venues</th></tr></thead><tbody>${mem}</tbody></table></div></div>
    <div class="panel"><h3>Excluded / skipped</h3><div class="scroll" style="max-height:480px"><table class="mini"><thead><tr><th class="num">Rank</th><th>Coin</th><th>Name</th><th>Reason</th></tr></thead><tbody>${exc}</tbody></table></div></div>`;
  $("#manual-form").addEventListener("submit", e => { e.preventDefault(); search(); });
  wire(el);
}

export async function load() {
  try {
    const r = await fetch("/api/universe", { cache: "no-store" });
    renderUniverse(await r.json());
  } catch (e) { $("#universe").textContent = "Universe unavailable: " + e; }
}

export function current() { return lastUniverse; }
