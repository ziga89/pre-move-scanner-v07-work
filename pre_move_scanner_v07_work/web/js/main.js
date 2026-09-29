// Router + wiring.
import { closeCoin, initCoin, openCoin, renderCoin } from "./coin.js";
import { Conn } from "./conn.js";
import { renderHealth } from "./health.js";
import { initTop, renderTop } from "./top.js";
import { load as loadUniverse } from "./universe.js";
import { $, $$ } from "./util.js";

let view = "top";
let coinTopic = null;
let version = "";           // from the server (canonical server/__init__.py); never hard-coded here
let currentAsset = null;

function setTitle() {
  const v = version ? ` v${version}` : "";
  document.title = currentAsset ? `${currentAsset} · Pre‑Move Scanner${v}` : `Pre‑Move Scanner${v}`;
}

function setVersion(v) {
  if (!v || v === version) return;
  version = v;
  $("#vtag").textContent = "v" + v;
  $("#footer-version").textContent = "Pre‑Move Scanner v" + v;
  setTitle();
}

const conn = new Conn(onMessage, onState);

function onState(s) {
  const dot = $("#dot");
  dot.classList.toggle("live", s === "live");
  dot.classList.toggle("poll", s === "poll");
  $("#connection").textContent = s === "live" ? "live" : s === "poll" ? "polling" : s;
}

function onMessage(topic, data) {
  if (topic === "top") {
    setVersion(data.version);
    renderTop(data);
    const m = $("#mode");
    m.textContent = (data.mode || "").toUpperCase();
    m.className = "modetag " + (data.mode || "");
  } else if (topic === "health") {
    setVersion(data.version);
    if (view === "health") renderHealth(data);
  } else if (topic && topic.startsWith("coin:")) {
    renderCoin(data);
  }
}

function route() {
  const h = location.hash || "#/";
  let next = "top";
  let asset = null;
  if (h.startsWith("#/coin/")) { next = "coin"; asset = decodeURIComponent(h.slice(7)).toUpperCase(); }
  else if (h.startsWith("#/health")) next = "health";
  else if (h.startsWith("#/universe")) next = "universe";
  view = next;
  $$(".view").forEach(s => { s.hidden = s.id !== "view-" + next; });
  $$("nav a").forEach(a => a.classList.toggle("active", a.dataset.view === next));
  if (coinTopic && (next !== "coin" || coinTopic !== "coin:" + asset)) {
    conn.unsubscribe(coinTopic);
    coinTopic = null;
    closeCoin();
  }
  if (next === "coin" && asset) {
    openCoin(asset);
    coinTopic = "coin:" + asset;
    conn.subscribe(coinTopic);
    currentAsset = asset;
  } else {
    currentAsset = null;
  }
  setTitle();
  if (next === "health") conn.subscribe("health"); else conn.unsubscribe("health");
  if (next === "health") fetch("/api/health", { cache: "no-store" }).then(r => r.json()).then(renderHealth).catch(() => {});
  if (next === "universe") loadUniverse();
  window.scrollTo(0, 0);
}

initTop();
initCoin();
fetch("/api/version", { cache: "no-store" }).then(r => r.json()).then(d => setVersion(d.version)).catch(() => {});
window.addEventListener("hashchange", route);
route();
conn.start();
if ("serviceWorker" in navigator && location.protocol === "https:") navigator.serviceWorker.register("/sw.js").catch(() => {});
