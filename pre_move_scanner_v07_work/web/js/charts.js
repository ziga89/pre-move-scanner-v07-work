// Canvas charts (dependency-free). Ported from v0.6 and extended:
// dual axes, gaps for missing data, dashed legacy (v0.6) series, and
// event markers coloured by category.
import { money } from "./util.js";

const PAL = ["#5eead4", "#fbbf24", "#fb7185", "#60a5fa", "#c084fc", "#34d399", "#f97316", "#a3e635"];
const EVENT_COL = {
  STATUS: "rgba(251,113,133,.7)", SCORE: "rgba(251,113,133,.55)", BOOK: "rgba(96,165,250,.55)",
  FLOW: "rgba(52,211,153,.5)", VENUE: "rgba(251,191,36,.55)", PRICE: "rgba(241,245,249,.55)",
  WALLET: "rgba(192,132,252,.6)", SYSTEM: "rgba(148,163,184,.3)", LEGACY: "rgba(148,163,184,.35)",
};

function setup(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(280, rect.width), h = Math.max(120, rect.height || 200);
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}
const fin = v => v !== null && v !== undefined && Number.isFinite(Number(v));

function niceTime(ts, spanH) {
  const d = new Date(ts * 1000);
  if (spanH <= 30) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit" });
}

export function empty(canvas, msg = "Collecting history…") {
  if (!canvas) return;
  const { ctx } = setup(canvas);
  ctx.fillStyle = "#94a3b8";
  ctx.font = "12px system-ui";
  ctx.fillText(msg, 52, 32);
}

function markers(ctx, events, X, pad, w, ph) {
  for (const e of (events || [])) {
    const x = X(Number(e.ts));
    if (x < pad.l || x > w - pad.r) continue;
    ctx.strokeStyle = EVENT_COL[e.category] || "rgba(148,163,184,.4)";
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ph); ctx.stroke();
    ctx.setLineDash([]);
  }
}

/**
 * series: [{label, get(row), color, axis:'left'|'right', dash, width, rows?}]
 * opts: {ymin, ymax, rmin, rmax, formatY, formatR, events, xmin, xmax}
 */
export function drawSeries(canvas, rows, series, opts = {}) {
  if (!canvas) return;
  const all = series.flatMap(s => (s.rows || rows || []));
  const xs = all.map(r => Number(r.ts)).filter(Number.isFinite);
  if (xs.length < 2) return empty(canvas);
  const { ctx, w, h } = setup(canvas);
  const hasRight = series.some(s => s.axis === "right");
  const pad = { l: 54, r: hasRight ? 48 : 14, t: 18, b: 26 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const xmin = opts.xmin ?? Math.min(...xs), xmax = opts.xmax ?? Math.max(...xs);
  const range = axis => {
    const vals = [];
    for (const s of series) {
      if ((s.axis || "left") !== axis) continue;
      for (const r of (s.rows || rows)) { const v = s.get(r); if (fin(v)) vals.push(Number(v)); }
    }
    const lo = axis === "left" ? opts.ymin : opts.rmin;
    const hi = axis === "left" ? opts.ymax : opts.rmax;
    if (!vals.length) return [lo ?? 0, hi ?? 1];
    let a = lo ?? Math.min(...vals), b = hi ?? Math.max(...vals);
    if (b === a) b = a + (Math.abs(a) || 1) * 0.01;
    const mg = (b - a) * 0.06;
    if (lo == null) a -= mg;
    if (hi == null) b += mg;
    return [a, b];
  };
  const [ly0, ly1] = range("left");
  const [ry0, ry1] = hasRight ? range("right") : [0, 1];
  const X = x => pad.l + (x - xmin) / ((xmax - xmin) || 1) * pw;
  const Y = (y, axis) => {
    const [a, b] = axis === "right" ? [ry0, ry1] : [ly0, ly1];
    return pad.t + (1 - (y - a) / ((b - a) || 1)) * ph;
  };
  const spanH = (xmax - xmin) / 3600;
  ctx.strokeStyle = "#263241"; ctx.lineWidth = 1; ctx.fillStyle = "#94a3b8"; ctx.font = "10px system-ui";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ph * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    const lv = ly1 - (ly1 - ly0) * i / 4;
    ctx.fillText(opts.formatY ? opts.formatY(lv) : String(+lv.toFixed(2)), 4, y + 3);
    if (hasRight) {
      const rv = ry1 - (ry1 - ry0) * i / 4;
      ctx.fillText(opts.formatR ? opts.formatR(rv) : String(+rv.toFixed(2)), w - pad.r + 4, y + 3);
    }
  }
  for (let i = 0; i <= 4; i++) {
    const x = pad.l + pw * i / 4;
    const label = niceTime(xmin + (xmax - xmin) * i / 4, spanH);
    const tw = ctx.measureText(label).width;
    ctx.fillText(label, Math.max(pad.l, Math.min(w - pad.r - tw, x - tw / 2)), h - 8);
  }
  markers(ctx, opts.events, X, pad, w, ph);
  const gap = Math.max(1, (xmax - xmin) / 60);
  series.forEach((s, idx) => {
    ctx.strokeStyle = s.color || PAL[idx % PAL.length];
    ctx.lineWidth = s.width || 1.7;
    ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    let pen = false, lastX = null;
    for (const r of (s.rows || rows)) {
      const v = s.get(r);
      const x = Number(r.ts);
      if (!fin(v)) { pen = false; continue; }
      if (lastX !== null && x - lastX > gap * 4) pen = false;  // data gap: break the line
      const px = X(x), py = Y(Number(v), s.axis || "left");
      if (!pen) { ctx.moveTo(px, py); pen = true; } else ctx.lineTo(px, py);
      lastX = x;
    }
    ctx.stroke();
    ctx.setLineDash([]);
  });
  let lx = pad.l + 4;
  ctx.font = "10px system-ui";
  series.forEach((s, idx) => {
    if (s.noLegend) return;
    ctx.fillStyle = s.color || PAL[idx % PAL.length];
    ctx.fillRect(lx, 5, 10, 2);
    ctx.fillStyle = "#cbd5e1";
    ctx.fillText(s.label, lx + 14, 9);
    lx += 18 + ctx.measureText(s.label).width + 12;
  });
}

export function drawVolume(canvas, rows, events) {
  if (!canvas) return;
  if (!rows || rows.length < 2) return empty(canvas);
  const { ctx, w, h } = setup(canvas);
  const pad = { l: 54, r: 30, t: 18, b: 26 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const xmin = rows[0].ts, xmax = rows[rows.length - 1].ts;
  const vols = rows.map(r => Number(r.volume_60s) || 0);
  const maxv = Math.max(...vols, 1);
  const X = x => pad.l + (x - xmin) / ((xmax - xmin) || 1) * pw;
  ctx.strokeStyle = "#263241"; ctx.fillStyle = "#94a3b8"; ctx.font = "10px system-ui";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ph * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(money(maxv * (1 - i / 4)), 4, y + 3);
  }
  const bw = Math.max(1, pw / rows.length);
  ctx.fillStyle = "rgba(96,165,250,.42)";
  rows.forEach(r => {
    const y = pad.t + (1 - (Number(r.volume_60s) || 0) / maxv) * ph;
    ctx.fillRect(X(r.ts), y, bw, Math.max(1, pad.t + ph - y));
  });
  const maxC = Math.max(...rows.map(r => Number(r.confirmed_venues) || 0), 1);
  ctx.strokeStyle = "#fbbf24"; ctx.lineWidth = 1.7; ctx.beginPath();
  rows.forEach((r, i) => {
    const y = pad.t + (1 - (Number(r.confirmed_venues) || 0) / maxC) * ph;
    if (i === 0) ctx.moveTo(X(r.ts), y); else ctx.lineTo(X(r.ts), y);
  });
  ctx.stroke();
  ctx.fillStyle = "#fbbf24";
  ctx.fillText(String(maxC), w - pad.r + 4, pad.t + 3);
  markers(ctx, events, X, pad, w, ph);
  ctx.fillStyle = "#cbd5e1"; ctx.fillText("Volume (60 s)", pad.l + 4, 10);
  ctx.fillStyle = "#fbbf24"; ctx.fillText("Confirmed venues", pad.l + 90, 10);
}

// Depth histogram across the ±band: bids left of centre, asks right of centre.
export function drawDepth(canvas, hist) {
  if (!canvas || !hist) return;
  const { ctx, w, h } = setup(canvas);
  const bins = (hist.bid || []).length;
  if (!bins) return;
  const max = Math.max(...hist.bid, ...hist.ask, 1);
  const half = w / 2, bw = half / bins;
  for (let i = 0; i < bins; i++) {
    const bh = ((hist.bid[i] || 0) / max) * (h - 4);
    ctx.fillStyle = "rgba(61,220,151,.6)";
    ctx.fillRect(half - (i + 1) * bw, h - bh, bw - 1, bh);
    const ah = ((hist.ask[i] || 0) / max) * (h - 4);
    ctx.fillStyle = "rgba(255,77,103,.6)";
    ctx.fillRect(half + i * bw, h - ah, bw - 1, ah);
  }
  ctx.fillStyle = "#475569";
  ctx.fillRect(half - 0.5, 0, 1, h);
}
