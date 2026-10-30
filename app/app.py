#!/usr/bin/env python3
"""Demo services for Project 3 (centralized logging).

One image, three roles picked by the SERVICE_NAME environment variable:

  shop-api        web shop API + control panel (port 8080). Generates its own
                  background traffic and calls the payments service.
  payments        internal payment service (port 9000, not published).
  alert-receiver  receives Grafana alert webhooks and shows them (port 5001).

Every role writes one logfmt line per event to stdout AND to
/var/log/app/<service>.log. Promtail tails those files and ships them to Loki.

Standard library only (no pip install), so the image builds from
python:3.12-slim with nothing else to download.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib import error as urlerror
from urllib import request as urlrequest

MAX_BODY = 64 * 1024
BRUTEFORCE_IP = "203.0.113.66"  # TEST-NET-3 documentation range
NORMAL_IPS = [f"198.51.100.{i}" for i in range(10, 30)]  # TEST-NET-2
USERS = ["alice", "bob", "carol", "dave", "erin"]
DEMO_PASSWORD = "demo"  # every simulated user's password (fake shop)
PRODUCTS = [
    {"sku": "TSHIRT-01", "name": "Logo T-shirt", "price": 20},
    {"sku": "MUG-02", "name": "Coffee mug", "price": 12},
    {"sku": "STICKER-03", "name": "Sticker pack", "price": 5},
]

# Which chaos modes each target accepts.
CHAOS_MODES = {
    "shop-api": {"normal", "errors", "bruteforce"},
    "payments": {"normal", "errors", "slow"},
}


# --------------------------------------------------------------------------
# logfmt logging
# --------------------------------------------------------------------------
def now_rfc3339() -> str:
    """UTC timestamp like 2026-10-29T16:51:00.123Z (parsed by Promtail)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fmt_value(value) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ' "=\t\n\\'):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return text


def logfmt(fields: dict) -> str:
    """Render a dict as one logfmt line. None values are skipped."""
    return " ".join(f"{k}={_fmt_value(v)}" for k, v in fields.items() if v is not None)


class EventLog:
    """Writes logfmt events to stdout and to <log_dir>/<service>.log."""

    def __init__(self, service: str, log_dir: str | None, stream=None):
        self.service = service
        self.logger = logging.getLogger(f"demo.{service}.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.handlers.clear()
        plain = logging.Formatter("%(message)s")
        out = logging.StreamHandler(stream or sys.stdout)
        out.setFormatter(plain)
        self.logger.addHandler(out)
        self.path = None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            self.path = os.path.join(log_dir, f"{service}.log")
            # Rotated files are named <service>.log.1 so Promtail's *.log glob skips them.
            fh = RotatingFileHandler(self.path, maxBytes=10_000_000, backupCount=2)
            fh.setFormatter(plain)
            self.logger.addHandler(fh)

    def emit(self, level: str, event: str, **fields) -> str:
        line = logfmt({"ts": now_rfc3339(), "level": level, "service": self.service,
                       "event": event, **fields})
        self.logger.info(line)
        return line

    def close(self):
        for h in list(self.logger.handlers):
            h.close()
            self.logger.removeHandler(h)


# --------------------------------------------------------------------------
# Chaos switch (time-limited failure modes)
# --------------------------------------------------------------------------
class Chaos:
    def __init__(self, allowed: set[str], clock=time.monotonic):
        self.allowed = allowed
        self.clock = clock
        self._mode = "normal"
        self._until = 0.0
        self._lock = threading.Lock()

    def set(self, mode: str, seconds: int = 90) -> None:
        if mode not in self.allowed:
            raise ValueError(f"mode must be one of {sorted(self.allowed)}")
        seconds = max(5, min(int(seconds), 600))
        with self._lock:
            self._mode = mode
            self._until = 0.0 if mode == "normal" else self.clock() + seconds

    def current(self) -> str:
        with self._lock:
            if self._mode != "normal" and self.clock() >= self._until:
                self._mode, self._until = "normal", 0.0
            return self._mode

    def remaining(self) -> int:
        mode = self.current()
        if mode == "normal":
            return 0
        with self._lock:
            return max(0, int(round(self._until - self.clock())))


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None,
              timeout: float = 5.0) -> tuple[int, dict]:
    """Tiny HTTP client. Returns (status, json_body). status 0 = connection error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urlerror.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urlerror.URLError, OSError, ValueError) as exc:
        return 0, {"error": str(exc)}


# --------------------------------------------------------------------------
# Payments service logic
# --------------------------------------------------------------------------
class Payments:
    def __init__(self, log: EventLog, rng: random.Random | None = None, sleep=time.sleep):
        self.log = log
        self.chaos = Chaos(CHAOS_MODES["payments"])
        self.rng = rng or random.Random()
        self.sleep = sleep

    def charge(self, order_id: str, amount: float, trace_id: str) -> tuple[int, dict]:
        start = time.monotonic()
        mode = self.chaos.current()
        if mode == "slow":
            self.sleep(self.rng.uniform(0.8, 1.6))
        else:
            self.sleep(self.rng.uniform(0.005, 0.04))
        ms = int((time.monotonic() - start) * 1000)
        if mode == "errors" and self.rng.random() < 0.7:
            self.log.emit("ERROR", "payment_error", trace_id=trace_id, order_id=order_id,
                          amount=amount, status=503, duration_ms=ms,
                          error="card gateway timeout")
            return 503, {"ok": False, "error": "card gateway timeout"}
        if self.rng.random() < 0.03:
            self.log.emit("WARN", "payment_declined", trace_id=trace_id, order_id=order_id,
                          amount=amount, status=402, duration_ms=ms, reason="insufficient_funds")
            return 402, {"ok": False, "error": "declined"}
        self.log.emit("INFO", "payment_ok", trace_id=trace_id, order_id=order_id,
                      amount=amount, status=200, duration_ms=ms)
        return 200, {"ok": True}


# --------------------------------------------------------------------------
# Shop service logic
# --------------------------------------------------------------------------
class Shop:
    def __init__(self, log: EventLog, payments_url: str, rng: random.Random | None = None):
        self.log = log
        self.payments_url = payments_url.rstrip("/")
        self.chaos = Chaos(CHAOS_MODES["shop-api"])
        self.rng = rng or random.Random()

    def products(self, trace_id: str, src_ip: str) -> tuple[int, dict]:
        ms = self.rng.randint(5, 60)
        if self.chaos.current() == "errors" and self.rng.random() < 0.4:
            self.log.emit("ERROR", "request", trace_id=trace_id, method="GET",
                          path="/api/products", status=500, duration_ms=ms, src_ip=src_ip,
                          error="db connection reset")
            return 500, {"error": "internal error"}
        self.log.emit("INFO", "request", trace_id=trace_id, method="GET", path="/api/products",
                      status=200, duration_ms=ms, src_ip=src_ip)
        return 200, {"products": PRODUCTS}

    def checkout(self, trace_id: str, src_ip: str) -> tuple[int, dict]:
        item = self.rng.choice(PRODUCTS)
        order_id = "ord-" + uuid.uuid4().hex[:8]
        start = time.monotonic()
        code, body = http_json("POST", f"{self.payments_url}/charge",
                               {"order_id": order_id, "amount": item["price"]},
                               headers={"X-Trace-Id": trace_id}, timeout=5)
        ms = int((time.monotonic() - start) * 1000) + self.rng.randint(2, 10)
        if code == 200:
            self.log.emit("INFO", "checkout", trace_id=trace_id, order_id=order_id,
                          sku=item["sku"], status=200, duration_ms=ms, src_ip=src_ip)
            return 200, {"ok": True, "order_id": order_id, "trace_id": trace_id}
        if code == 402:
            self.log.emit("WARN", "checkout_failed", trace_id=trace_id, order_id=order_id,
                          status=402, duration_ms=ms, src_ip=src_ip, reason="payment declined")
            return 402, {"ok": False, "error": "payment declined", "trace_id": trace_id}
        self.log.emit("ERROR", "checkout_failed", trace_id=trace_id, order_id=order_id,
                      status=502, duration_ms=ms, src_ip=src_ip, upstream="payments",
                      upstream_status=code, error=body.get("error", "upstream error"))
        return 502, {"ok": False, "error": "payment service failed", "trace_id": trace_id}

    def login(self, user: str, password: str, src_ip: str, trace_id: str) -> tuple[int, dict]:
        # Never log the password itself - only the outcome.
        if user in USERS and password == DEMO_PASSWORD:
            self.log.emit("INFO", "login_ok", trace_id=trace_id, user=user, src_ip=src_ip,
                          status=200)
            return 200, {"ok": True}
        self.log.emit("WARN", "login_failed", trace_id=trace_id, user=user, src_ip=src_ip,
                      status=401, reason="bad credentials")
        return 401, {"ok": False, "error": "invalid credentials"}

    def simulate_once(self) -> None:
        """One unit of background traffic."""
        trace_id = new_trace_id()
        ip = self.rng.choice(NORMAL_IPS)
        roll = self.rng.random()
        if roll < 0.5:
            self.products(trace_id, ip)
        elif roll < 0.8:
            self.checkout(trace_id, ip)
        else:
            user = self.rng.choice(USERS)
            pw = DEMO_PASSWORD if self.rng.random() > 0.1 else "typo"
            self.login(user, pw, ip, trace_id)
        if self.chaos.current() == "bruteforce":
            self.login(self.rng.choice(["admin", "root", "alice"]),
                       "guess" + str(self.rng.randint(1, 99999)), BRUTEFORCE_IP, new_trace_id())


# --------------------------------------------------------------------------
# Alert receiver logic
# --------------------------------------------------------------------------
class AlertInbox:
    def __init__(self, log: EventLog, maxlen: int = 100):
        self.log = log
        self.items: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def receive(self, payload: dict) -> int:
        """Store alerts from a Grafana webhook payload. Returns how many were stored."""
        count = 0
        for alert in payload.get("alerts", []) or []:
            labels = alert.get("labels", {}) or {}
            ann = alert.get("annotations", {}) or {}
            item = {
                "received_at": now_rfc3339(),
                "status": alert.get("status", payload.get("status", "unknown")),
                "alertname": labels.get("alertname", "unknown"),
                "severity": labels.get("severity", ""),
                "src_ip": labels.get("src_ip", ""),
                "summary": ann.get("summary", ""),
                "starts_at": alert.get("startsAt", ""),
            }
            with self._lock:
                self.items.appendleft(item)
            level = "WARN" if item["status"] == "firing" else "INFO"
            self.log.emit(level, "alert_received", alertname=item["alertname"],
                          status=item["status"], severity=item["severity"],
                          src_ip=item["src_ip"] or None, summary=item["summary"])
            count += 1
        return count

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.items)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "logging-demo/1.0"
    role = "shop-api"
    shop: Shop | None = None
    payments: Payments | None = None
    inbox: AlertInbox | None = None
    peer_url = ""

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    # helpers ---------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _html(self, text: str):
        self._send(200, text.encode(), "text/html; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def _src_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    # routing -----------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json(200, {"status": "ok", "service": self.role})
        if self.role == "shop-api":
            if path == "/":
                return self._html(SHOP_PAGE)
            if path == "/api/products":
                code, body = self.shop.products(new_trace_id(), self._src_ip())
                return self._json(code, body)
            if path == "/api/status":
                pcode, pbody = http_json("GET", f"{self.peer_url}/api/status", timeout=3)
                return self._json(200, {
                    "service": "shop-api", "mode": self.shop.chaos.current(),
                    "remaining": self.shop.chaos.remaining(),
                    "payments": pbody if pcode == 200 else {"ok": False, "error": "unreachable"},
                })
        if self.role == "payments" and path == "/api/status":
            return self._json(200, {"ok": True, "service": "payments",
                                    "mode": self.payments.chaos.current(),
                                    "remaining": self.payments.chaos.remaining()})
        if self.role == "alert-receiver":
            if path == "/":
                return self._html(INBOX_PAGE)
            if path == "/api/alerts":
                items = self.inbox.snapshot()
                return self._json(200, {"count": len(items), "alerts": items})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": str(exc)})

        if self.role == "shop-api":
            if path == "/api/checkout":
                code, resp = self.shop.checkout(new_trace_id(), self._src_ip())
                return self._json(code, resp)
            if path == "/api/login":
                code, resp = self.shop.login(str(body.get("user", ""))[:64],
                                             str(body.get("password", "")),
                                             self._src_ip(), new_trace_id())
                return self._json(code, resp)
            if path == "/api/chaos":
                target = body.get("target", "shop-api")
                mode = str(body.get("mode", "normal"))
                seconds = body.get("seconds", 90)
                if target == "payments":
                    code, resp = http_json("POST", f"{self.peer_url}/api/chaos",
                                           {"mode": mode, "seconds": seconds}, timeout=3)
                    if code == 200:
                        self.shop.log.emit("WARN", "chaos_set", target="payments", mode=mode,
                                           seconds=seconds)
                    return self._json(code or 502, resp)
                if target != "shop-api":
                    return self._json(400, {"error": "target must be shop-api or payments"})
                try:
                    self.shop.chaos.set(mode, int(seconds))
                except (ValueError, TypeError) as exc:
                    return self._json(400, {"error": str(exc)})
                self.shop.log.emit("WARN", "chaos_set", target="shop-api", mode=mode,
                                   seconds=seconds)
                return self._json(200, {"ok": True, "target": "shop-api", "mode": mode})

        if self.role == "payments":
            if path == "/charge":
                trace_id = self.headers.get("X-Trace-Id") or new_trace_id()
                code, resp = self.payments.charge(str(body.get("order_id", "unknown")),
                                                  body.get("amount", 0), trace_id)
                return self._json(code, resp)
            if path == "/api/chaos":
                try:
                    self.payments.chaos.set(str(body.get("mode", "normal")),
                                            int(body.get("seconds", 90)))
                except (ValueError, TypeError) as exc:
                    return self._json(400, {"error": str(exc)})
                self.payments.log.emit("WARN", "chaos_set", target="payments",
                                       mode=self.payments.chaos.current())
                return self._json(200, {"ok": True, "target": "payments",
                                        "mode": self.payments.chaos.current()})

        if self.role == "alert-receiver" and path == "/alert":
            n = self.inbox.receive(body)
            return self._json(200, {"stored": n})

        return self._json(404, {"error": "not found"})


def build_handler(role: str, log: EventLog, peer_url: str = "") -> type[Handler]:
    attrs = {"role": role, "peer_url": peer_url.rstrip("/")}
    if role == "shop-api":
        attrs["shop"] = Shop(log, peer_url)
    elif role == "payments":
        attrs["payments"] = Payments(log)
    elif role == "alert-receiver":
        attrs["inbox"] = AlertInbox(log)
    else:
        raise ValueError(f"unknown SERVICE_NAME {role!r}")
    return type("BoundHandler", (Handler,), attrs)


def traffic_worker(shop: Shop, stop: threading.Event, rng: random.Random) -> None:
    while not stop.is_set():
        try:
            shop.simulate_once()
        except Exception as exc:  # keep generating even if one step fails
            shop.log.emit("ERROR", "traffic_generator_error", error=repr(exc))
        stop.wait(rng.uniform(0.4, 1.2))


def main() -> None:
    role = os.environ.get("SERVICE_NAME", "shop-api")
    port = int(os.environ.get("PORT", "8080"))
    log = EventLog(role, os.environ.get("LOG_DIR", "/var/log/app"))
    peer = os.environ.get("PAYMENTS_URL", "http://payments:9000")
    handler = build_handler(role, log, peer)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    log.emit("INFO", "service_started", port=port)
    stop = threading.Event()
    if role == "shop-api" and os.environ.get("TRAFFIC", "1") == "1":
        workers = int(os.environ.get("TRAFFIC_WORKERS", "3"))
        for i in range(workers):
            threading.Thread(target=traffic_worker, args=(handler.shop, stop, random.Random(i)),
                             daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        log.emit("INFO", "service_stopped")


# --------------------------------------------------------------------------
# Pages (links to other UIs use the hostname the browser already used)
# --------------------------------------------------------------------------
_STYLE = """
:root{--bg:#0f1720;--card:#17212b;--fg:#e6edf3;--muted:#8b98a5;--accent:#f2a33a;--bad:#e5534b;--ok:#3fb950}
@media (prefers-color-scheme: light){:root{--bg:#f6f8fa;--card:#fff;--fg:#1f2328;--muted:#59636e}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px} p.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border-radius:10px;padding:16px;margin-bottom:16px}
button{background:var(--accent);color:#111;border:0;border-radius:8px;padding:9px 14px;
 margin:4px 6px 4px 0;font-weight:600;cursor:pointer} button.alt{background:#30363d;color:#e6edf3}
code,pre{font-family:ui-monospace,monospace;font-size:13px} pre{white-space:pre-wrap;margin:0}
a{color:var(--accent)} table{width:100%;border-collapse:collapse} td,th{text-align:left;
 padding:6px 8px;border-bottom:1px solid #30363d;font-size:14px}
.firing{color:var(--bad);font-weight:700}.resolved{color:var(--ok);font-weight:700}
"""

SHOP_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Shop control panel</title>
<style>""" + _STYLE + """</style></head><body><main>
<h1>Demo shop &mdash; control panel</h1>
<p class="sub">The shop generates traffic on its own. Use these buttons to break things,
then watch the logs and alerts.</p>
<div class="card"><b>Open:</b>
 <a id="g" href="#">Grafana dashboard (:3000)</a> &middot; <a id="a" href="#">Alert inbox (:5001)</a></div>
<div class="card"><b>Break something (90 s)</b><br>
 <button onclick="chaos('payments','errors')">Payments outage</button>
 <button onclick="chaos('shop-api','errors')">Shop error burst</button>
 <button onclick="chaos('payments','slow')">Slow payments</button>
 <button onclick="chaos('shop-api','bruteforce')">Brute-force login attack</button>
 <button class="alt" onclick="normal()">Back to normal</button></div>
<div class="card"><b>Single actions</b><br>
 <button class="alt" onclick="call('POST','/api/checkout')">Place one order</button>
 <button class="alt" onclick="call('GET','/api/products')">List products</button></div>
<div class="card"><b>Status</b><pre id="status">loading...</pre></div>
<div class="card"><b>Last response</b><pre id="out">-</pre></div>
</main><script>
const h=location.hostname;
document.getElementById('g').href='http://'+h+':3000/d/logging-overview';
document.getElementById('a').href='http://'+h+':5001/';
async function call(m,u,b){const r=await fetch(u,{method:m,headers:{'Content-Type':'application/json'},
 body:b?JSON.stringify(b):undefined});const t=await r.text();
 document.getElementById('out').textContent=r.status+' '+t;refresh();}
function chaos(t,m){call('POST','/api/chaos',{target:t,mode:m,seconds:90});}
async function normal(){await call('POST','/api/chaos',{target:'shop-api',mode:'normal'});
 await call('POST','/api/chaos',{target:'payments',mode:'normal'});}
async function refresh(){try{const r=await fetch('/api/status');
 document.getElementById('status').textContent=JSON.stringify(await r.json(),null,2);}catch(e){}}
refresh();setInterval(refresh,3000);
</script></body></html>"""

INBOX_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Alert inbox</title>
<style>""" + _STYLE + """</style></head><body><main>
<h1>Alert inbox</h1>
<p class="sub">Grafana sends alert notifications here by webhook. Refreshes every 5 s.
 <a id="s" href="#">Shop control panel</a></p>
<div class="card"><table><thead><tr><th>Received (UTC)</th><th>Status</th><th>Alert</th>
<th>Severity</th><th>Summary</th></tr></thead><tbody id="rows">
<tr><td colspan="5">No alerts yet.</td></tr></tbody></table></div>
</main><script>
document.getElementById('s').href='http://'+location.hostname+':8080/';
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function load(){try{const r=await fetch('/api/alerts');const d=await r.json();
 if(!d.alerts.length)return;document.getElementById('rows').innerHTML=d.alerts.map(a=>
 '<tr><td>'+esc(a.received_at.replace('T',' ').slice(0,19))+'</td><td class="'+esc(a.status)+'">'+
 esc(a.status)+'</td><td>'+esc(a.alertname)+(a.src_ip?' ('+esc(a.src_ip)+')':'')+'</td><td>'+
 esc(a.severity)+'</td><td>'+esc(a.summary)+'</td></tr>').join('');}catch(e){}}
load();setInterval(load,5000);
</script></body></html>"""


if __name__ == "__main__":
    main()
