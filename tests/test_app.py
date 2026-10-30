"""Unit tests for app/app.py (no Docker needed)."""
import io
import json
import random
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app  # noqa: E402


# ---------------------------------------------------------------- logfmt
def test_logfmt_plain_and_quoted_values():
    line = app.logfmt({"a": 1, "b": "x y", "c": 'say "hi"', "d": None, "e": ""})
    assert line == 'a=1 b="x y" c="say \\"hi\\"" e=""'


def test_timestamp_is_rfc3339_utc():
    ts = app.now_rfc3339()
    assert ts.endswith("Z") and "T" in ts and len(ts) == 24


def test_eventlog_writes_file_and_stdout(tmp_path):
    out = io.StringIO()
    log = app.EventLog("shop-api", str(tmp_path), stream=out)
    line = log.emit("ERROR", "request", status=500, error="db down")
    log.close()
    text = (tmp_path / "shop-api.log").read_text()
    assert line in text and line in out.getvalue()
    assert "level=ERROR" in line and "service=shop-api" in line and 'error="db down"' in line


# ---------------------------------------------------------------- chaos
def test_chaos_expires_and_validates():
    now = [100.0]
    c = app.Chaos({"normal", "errors"}, clock=lambda: now[0])
    c.set("errors", 30)
    assert c.current() == "errors" and c.remaining() == 30
    now[0] += 31
    assert c.current() == "normal" and c.remaining() == 0
    with pytest.raises(ValueError):
        c.set("bruteforce", 10)


def test_chaos_clamps_duration():
    now = [0.0]
    c = app.Chaos({"normal", "errors"}, clock=lambda: now[0])
    c.set("errors", 99999)
    assert c.remaining() == 600


# ---------------------------------------------------------------- services
def make_log(tmp_path, name):
    return app.EventLog(name, str(tmp_path), stream=io.StringIO())


def test_payments_errors_mode_returns_503(tmp_path):
    p = app.Payments(make_log(tmp_path, "payments"), rng=random.Random(1), sleep=lambda s: None)
    p.chaos.set("errors", 60)
    codes = [p.charge("o1", 10, "t1")[0] for _ in range(50)]
    assert codes.count(503) > 20
    assert "event=payment_error" in (tmp_path / "payments.log").read_text()


def test_payments_normal_mostly_ok(tmp_path):
    p = app.Payments(make_log(tmp_path, "payments"), rng=random.Random(2), sleep=lambda s: None)
    codes = [p.charge("o1", 10, "t1")[0] for _ in range(100)]
    assert codes.count(200) > 90 and 503 not in codes


def test_login_never_logs_password(tmp_path):
    shop = app.Shop(make_log(tmp_path, "shop-api"), "http://unused")
    assert shop.login("alice", "demo", "1.2.3.4", "t")[0] == 200
    assert shop.login("alice", "SuperSecret123", "1.2.3.4", "t")[0] == 401
    text = (tmp_path / "shop-api.log").read_text()
    assert "SuperSecret123" not in text
    assert "event=login_failed" in text and "src_ip=1.2.3.4" in text


def test_bruteforce_mode_adds_failed_logins_from_attacker_ip(tmp_path):
    shop = app.Shop(make_log(tmp_path, "shop-api"), "http://127.0.0.1:9", rng=random.Random(3))
    shop.chaos.set("bruteforce", 60)
    for _ in range(20):
        shop.simulate_once()
    text = (tmp_path / "shop-api.log").read_text()
    assert text.count(f"src_ip={app.BRUTEFORCE_IP}") >= 20


def test_alert_inbox_parses_grafana_payload(tmp_path):
    inbox = app.AlertInbox(make_log(tmp_path, "alert-receiver"))
    payload = {"status": "firing", "alerts": [
        {"status": "firing", "labels": {"alertname": "BruteForceLogin", "severity": "high",
                                         "src_ip": "203.0.113.66"},
         "annotations": {"summary": "many failed logins"}, "startsAt": "2026-10-29T10:00:00Z"},
        {"status": "resolved", "labels": {"alertname": "HighErrorRate"}, "annotations": {}},
    ]}
    assert inbox.receive(payload) == 2
    items = inbox.snapshot()
    assert items[0]["alertname"] == "HighErrorRate" and items[0]["status"] == "resolved"
    assert items[1]["src_ip"] == "203.0.113.66"
    assert inbox.receive({}) == 0


def test_build_handler_rejects_unknown_role(tmp_path):
    with pytest.raises(ValueError):
        app.build_handler("nope", make_log(tmp_path, "x"))


# ---------------------------------------------------------------- real HTTP
def serve(role, log, peer=""):
    handler = app.build_handler(role, log, peer)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", handler


@pytest.fixture
def stack(tmp_path):
    pay_srv, pay_url, pay_h = serve("payments", make_log(tmp_path, "payments"))
    pay_h.payments.sleep = lambda s: None
    shop_srv, shop_url, shop_h = serve("shop-api", make_log(tmp_path, "shop-api"), pay_url)
    rcv_srv, rcv_url, _ = serve("alert-receiver", make_log(tmp_path, "alert-receiver"))
    yield {"shop": shop_url, "pay": pay_url, "rcv": rcv_url, "dir": tmp_path}
    for s in (pay_srv, shop_srv, rcv_srv):
        s.shutdown()
        s.server_close()


def call(method, url, body=None):
    return app.http_json(method, url, body, timeout=5)


def test_http_health_and_pages(stack):
    for key in ("shop", "pay", "rcv"):
        assert call("GET", stack[key] + "/health")[0] == 200
    with request.urlopen(stack["shop"] + "/") as r:
        assert b"control panel" in r.read()
    with request.urlopen(stack["rcv"] + "/") as r:
        assert b"Alert inbox" in r.read()
    assert call("GET", stack["shop"] + "/nope")[0] == 404


def test_http_checkout_flows_through_payments_with_same_trace_id(stack):
    code, body = call("POST", stack["shop"] + "/api/checkout")
    assert code in (200, 402)
    tid = body["trace_id"]
    time.sleep(0.05)
    assert f"trace_id={tid}" in (stack["dir"] / "shop-api.log").read_text()
    assert f"trace_id={tid}" in (stack["dir"] / "payments.log").read_text()


def test_http_payments_outage_makes_checkout_fail(stack):
    code, body = call("POST", stack["shop"] + "/api/chaos",
                      {"target": "payments", "mode": "errors", "seconds": 30})
    assert code == 200 and body["mode"] == "errors"
    codes = [call("POST", stack["shop"] + "/api/checkout")[0] for _ in range(30)]
    assert codes.count(502) > 10
    status = call("GET", stack["shop"] + "/api/status")[1]
    assert status["payments"]["mode"] == "errors"
    shop_log = (stack["dir"] / "shop-api.log").read_text()
    assert "upstream=payments" in shop_log and "level=ERROR" in shop_log


def test_http_chaos_validation(stack):
    assert call("POST", stack["shop"] + "/api/chaos", {"target": "shop-api", "mode": "slow"})[0] == 400
    assert call("POST", stack["shop"] + "/api/chaos", {"target": "db", "mode": "errors"})[0] == 400
    assert call("POST", stack["shop"] + "/api/chaos", {"target": "payments", "mode": "bruteforce"})[0] == 400


def test_http_bad_json_is_400(stack):
    req = request.Request(stack["shop"] + "/api/login", data=b"[1,2]", method="POST",
                          headers={"Content-Type": "application/json"})
    with pytest.raises(Exception) as exc:
        request.urlopen(req)
    assert "400" in str(exc.value)


def test_http_alert_webhook_roundtrip(stack):
    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "HighErrorRate"},
                           "annotations": {"summary": "x"}}]}
    assert call("POST", stack["rcv"] + "/alert", payload) == (200, {"stored": 1})
    code, body = call("GET", stack["rcv"] + "/api/alerts")
    assert code == 200 and body["count"] == 1 and body["alerts"][0]["status"] == "firing"
    assert "event=alert_received" in (stack["dir"] / "alert-receiver.log").read_text()


def test_http_client_reports_connection_error():
    assert app.http_json("GET", "http://127.0.0.1:9/x", timeout=1)[0] == 0
