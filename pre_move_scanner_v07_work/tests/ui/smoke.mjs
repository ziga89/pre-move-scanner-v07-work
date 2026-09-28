// Headless-browser smoke test of the web UI against a running server.
// Usage: node tests/ui/smoke.mjs http://127.0.0.1:8013 <screenshot-dir>
import { createRequire } from "module";
const require = createRequire(import.meta.url);
let playwright;
try { playwright = require("playwright"); } catch (e) {
  const g = require("child_process").execSync("npm root -g").toString().trim();
  playwright = require(g + "/playwright");
}
const base = process.argv[2] || "http://127.0.0.1:8013";
const shots = process.argv[3] || ".";
const fails = [];
const check = (ok, msg) => { if (!ok) fails.push(msg); console.log((ok ? "PASS " : "FAIL ") + msg); };

const opts = { headless: true };
if (process.env.PW_CHROMIUM) opts.executablePath = process.env.PW_CHROMIUM;
const browser = await playwright.chromium.launch(opts);
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const errors = [];
page.on("pageerror", e => errors.push("pageerror: " + e.message));
page.on("console", m => {
  // The stdlib dev server has no WebSocket: the 426 handshake failure is the designed fallback trigger.
  if (m.type() === "error" && !/WebSocket connection .* 426/.test(m.text())) errors.push("console: " + m.text());
});
page.on("response", r => { if (r.status() >= 400 && !r.url().endsWith("/ws")) errors.push(`HTTP ${r.status()} ${r.url()}`); });

await page.goto(base + "/", { waitUntil: "domcontentloaded" });
await page.waitForSelector("#top-table tbody tr", { timeout: 30000 });
const nrows = await page.$$eval("#top-table tbody tr", r => r.length);
check(nrows >= 5, `anomaly table has rows (${nrows})`);
check((await page.textContent("#mode")).trim() === "SIM", "mode tag shows SIM");
await page.waitForTimeout(3000);
check(["polling", "live"].includes((await page.textContent("#connection")).trim()), "live data connection (ws or polling fallback)");
const headers = await page.$$eval("#top-table thead th", t => t.map(x => x.textContent.trim()));
for (const h of ["Pre‑Move", "Liquidity", "MM", "Whale", "CEX flow", "15m", "1h", "Venues", "Reason", "Status"])
  check(headers.includes(h), `column ${h}`);
check((await page.$$(".na")).length > 0, "missing wallet intelligence rendered as N/A");
await page.screenshot({ path: shots + "/ui_top.png", fullPage: false });

// filters
await page.fill("#f-search", "zzzz");
await page.waitForTimeout(300);
const visible = await page.$$eval("#top-table tbody tr:not(.hidden)", r => r.length);
check(visible === 0, "search filter hides non-matching rows");
await page.fill("#f-search", "");

// coin detail
const first = await page.$eval("#top-table tbody tr", tr => tr.dataset.asset);
await page.click("#top-table tbody tr");
await page.waitForSelector("#view-coin:not([hidden])", { timeout: 5000 });
await page.waitForFunction(() => document.querySelector("#coin-head").textContent.length > 20, null, { timeout: 20000 });
check((await page.textContent("#coin-head")).includes(first), `coin view opens for ${first}`);
await page.waitForSelector("#coin-venues table tbody tr", { timeout: 20000 });
check((await page.$$("#coin-venues tbody tr")).length >= 1, "venue table rendered");
check((await page.textContent("#coin-pipeline")).includes("Late‑move multiplier"), "score pipeline rendered");
check((await page.textContent("#coin-wallet")).includes("disabled"), "wallet panel explains disabled intel");
const cw = await page.$eval("#ch-price", c => c.width);
check(cw > 200, "price/score chart drawn");
for (const h of ["1", "24", "168"]) {
  await page.click(`.range-buttons button[data-hours="${h}"]`);
  await page.waitForTimeout(700);
}
check(!(await page.textContent("#history-subtitle")).includes("error"), "history ranges load");
await page.screenshot({ path: shots + "/ui_coin.png", fullPage: true });

// health + universe
await page.goto(base + "/#/health");
await page.waitForSelector("#health table", { timeout: 10000 });
check((await page.textContent("#health")).includes("simex_"), "health lists exchanges");
await page.screenshot({ path: shots + "/ui_health.png", fullPage: true });
await page.goto(base + "/#/universe");
await page.waitForSelector("#universe table", { timeout: 10000 });
check((await page.$$("#universe tbody tr")).length >= 5, "universe members listed");

// mobile: no horizontal page scroll
const m = await browser.newPage({ viewport: { width: 390, height: 844 } });
m.on("pageerror", e => errors.push("mobile pageerror: " + e.message));
await m.goto(base + "/");
await m.waitForSelector("#top-table tbody tr", { timeout: 30000 });
const overflow = await m.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
check(overflow <= 1, `no horizontal page scroll at 390px (overflow ${overflow}px)`);
await m.screenshot({ path: shots + "/ui_mobile.png" });
await m.goto(base + "/#/coin/" + encodeURIComponent(first));
await m.waitForSelector("#coin-venues table", { timeout: 20000 });
const overflow2 = await m.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
check(overflow2 <= 1, `coin view: no horizontal page scroll at 390px (overflow ${overflow2}px)`);

check(errors.length === 0, "no JS errors: " + errors.join(" | "));
await browser.close();
console.log(fails.length ? `\n${fails.length} UI check(s) FAILED` : "\nALL UI CHECKS PASSED");
process.exit(fails.length ? 1 : 0);
