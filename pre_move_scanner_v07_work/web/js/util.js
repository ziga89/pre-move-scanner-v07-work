// Shared formatting helpers.
export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

export function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
export function isNum(v) { return typeof v === "number" && Number.isFinite(v); }
export function money(v) {
  if (!isNum(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
  return "$" + v.toFixed(0);
}
export function num(v, d = 1) { return isNum(v) ? v.toFixed(d) : "—"; }
export function signedPct(v, d = 1) {          // v already in percent
  if (!isNum(v)) return "—";
  return (v > 0 ? "+" : "") + v.toFixed(d) + "%";
}
export function ratioPct(r, d = 0) {           // ratio (1.0 = normal) -> "-42%"
  if (!isNum(r)) return "—";
  const p = (r - 1) * 100;
  return (p > 0 ? "+" : "") + p.toFixed(d) + "%";
}
export function share(v, d = 0) { return isNum(v) ? (v * 100).toFixed(d) + "%" : "—"; }
export function price(v) {
  if (!isNum(v)) return "—";
  return "$" + v.toLocaleString(undefined, { maximumFractionDigits: v < 1 ? 7 : v < 100 ? 4 : 2 });
}
export function scoreColor(s) {
  if (!isNum(s)) return "#475569";
  if (s >= 80) return "var(--extreme)";
  if (s >= 70) return "var(--hot)";
  if (s >= 55) return "var(--warn)";
  if (s >= 40) return "var(--blue)";
  return "#475569";
}
export function na(tip = "No reliable data — shown as N/A, never counted as 0") {
  return `<span class="na" title="${esc(tip)}">N/A</span>`;
}
export function scoreCell(v, withBar = true) {
  if (!isNum(v)) return na();
  const bar = withBar ? `<span class="bar"><i style="width:${Math.min(100, v)}%;background:${scoreColor(v)}"></i></span>` : "";
  return `<span class="scorecell">${bar}<b style="color:${scoreColor(v)}">${v.toFixed(0)}</b></span>`;
}
const STATUS_CLS = {
  "STRONG PRE-MOVE": "st-strong", "CONFIRMED PRE-MOVE": "st-confirmed", "EMERGING": "st-emerging",
  "WATCH": "st-watch", "LATE": "st-late", "MOVE IN PROGRESS": "st-moving", "LOW CONFIDENCE": "st-lowconf",
  "NORMAL": "st-quiet", "WARMING": "st-quiet", "STALE": "st-quiet", "NO DATA": "st-quiet",
};
export function badge(st) { return `<span class="badge ${STATUS_CLS[st] || ""}">${esc(st || "—")}</span>`; }
export function fmtTime(ts) { return isNum(ts) ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"; }
export function fmtDateTime(ts) {
  return isNum(ts) ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}
export function ago(ts) {
  if (!isNum(ts)) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 172800) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}
