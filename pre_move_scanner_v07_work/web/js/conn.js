// Live data: WebSocket topics with automatic fallback to HTTP polling
// (the stdlib dev server has no WebSocket). At most one reconnect timer —
// v0.6 leaked a new ping interval on every reconnect.
export class Conn {
  constructor(onMessage, onState) {
    this.onMessage = onMessage;
    this.onState = onState;
    this.topics = new Set(["top"]);
    this.ws = null;
    this.failures = 0;
    this.mode = "ws";
    this.timer = null;
    this.pollTimer = null;
  }

  start() {
    if (this.mode === "ws") this._ws();
    else this._poll();
  }

  subscribe(t) {
    this.topics.add(t);
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify({ subscribe: [t] }));
    if (this.mode === "poll") this._pollOnce(t);
  }

  unsubscribe(t) {
    this.topics.delete(t);
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify({ unsubscribe: [t] }));
  }

  _ws() {
    let opened = false;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      this.ws = new WebSocket(`${proto}//${location.host}/ws`);
    } catch (e) {
      this._fallback();
      return;
    }
    this.ws.onopen = () => {
      opened = true;
      this.failures = 0;
      this.onState("live");
      this.ws.send(JSON.stringify({ subscribe: [...this.topics] }));
    };
    this.ws.onmessage = e => {
      try {
        const m = JSON.parse(e.data);
        this.onMessage(m.topic, m.data);
      } catch (err) {
        console.error(err);
      }
    };
    this.ws.onclose = () => {
      this.onState("reconnecting");
      if (!opened) this.failures += 1;
      if (this.failures >= 2) {
        this._fallback();
        return;
      }
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this._ws(), Math.min(10000, 1500 * (1 + this.failures)));
    };
    this.ws.onerror = () => {
      try { this.ws.close(); } catch (e) { /* already closed */ }
    };
  }

  _fallback() {
    this.mode = "poll";
    this._poll();
  }

  async _pollOnce(t) {
    let url = null;
    if (t === "top") url = "/api/top";
    else if (t === "health") url = "/api/health";
    else if (t.startsWith("coin:")) url = `/api/coin/${encodeURIComponent(t.slice(5))}`;
    if (!url) return;
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) return;
      this.onMessage(t, await r.json());
      this.onState("poll");
    } catch (e) {
      this.onState("offline");
    }
  }

  _poll() {
    clearInterval(this.pollTimer);
    const tick = () => { for (const t of this.topics) this._pollOnce(t); };
    tick();
    this.pollTimer = setInterval(tick, 2500);
  }
}
