"""Exercise the bash scripts with stub `curl`, `docker` and `sudo` commands on PATH.

The stubs answer like the real services do (including Docker Hub's 401), so the
script logic is tested here even though Docker cannot run in this environment.
"""
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_CURL = textwrap.dedent(r'''
    #!/usr/bin/env python3
    """Fake curl: answers from a JSON route table, honours -o /dev/null, -w, -X."""
    import json, os, sys
    args = sys.argv[1:]
    routes = json.load(open(os.environ["FAKE_ROUTES"]))
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write("curl " + " ".join(args) + "\n")
    url = next(a for a in args if a.startswith("http"))
    method = args[args.index("-X") + 1] if "-X" in args else "GET"
    key = url.split("?")[0]
    route = routes.get(method + " " + key) or routes.get(key)
    write_out = args[args.index("-w") + 1] if "-w" in args else None
    if route is None or route.get("fail"):
        if write_out:
            sys.stdout.write("000")
        sys.exit(7)
    code = str(route.get("code", 200))
    body = route.get("body", "")
    if not isinstance(body, str):
        body = json.dumps(body)
    if not ("-o" in args and args[args.index("-o") + 1] == "/dev/null"):
        sys.stdout.write(body)
    if write_out:
        sys.stdout.write(write_out.replace("%{http_code}", code))
''').lstrip()

FAKE_DOCKER = textwrap.dedent(r'''
    #!/usr/bin/env bash
    echo "docker $*" >> "$FAKE_LOG"
    case "$*" in
      info*) exit 0 ;;
      "image inspect"*) [ -z "${FAKE_NO_IMAGE:-}" ] ;;
      "compose config --services"*) printf '%s\n' loki promtail grafana shop-api payments alert-receiver ;;
      "compose ps --services --status running"*) printf '%s\n' ${FAKE_RUNNING:-loki promtail grafana shop-api payments alert-receiver} ;;
      *) exit 0 ;;
    esac
''').lstrip()

FAKE_SUDO = '#!/usr/bin/env bash\nshift_args=("$@"); [ "$1" = "-n" ] && shift; exec "$@"\n'

GOOD_ROUTES = {
    "PUT http://169.254.169.254/latest/api/token": {"body": "TOKEN123"},
    "http://169.254.169.254/latest/meta-data/public-ipv4": {"body": "54.1.2.3"},
    "https://checkip.amazonaws.com": {"body": "9.9.9.9\n"},
    "https://registry-1.docker.io/v2/": {"code": 401, "body": "{\"errors\":[]}"},
    "http://127.0.0.1:3100/ready": {"body": "ready"},
    "http://127.0.0.1:9080/ready": {"body": "Ready"},
    "http://127.0.0.1:3000/api/health": {"body": {"database": "ok"}},
    "http://127.0.0.1:3000/api/datasources/uid/loki/health": {"body": {"status": "OK"}},
    "http://127.0.0.1:3000/api/dashboards/uid/logging-overview": {"body": {"dashboard": {"uid": "logging-overview"}}},
    "http://127.0.0.1:3000/api/v1/provisioning/alert-rules": {"body": [{}, {}, {}]},
    "http://127.0.0.1:3000/api/v1/provisioning/contact-points": {"body": [{"name": "demo-webhook"}]},
    "http://127.0.0.1:8080/health": {"body": {"status": "ok"}},
    "http://127.0.0.1:8080/api/status": {"body": {"payments": {"ok": True}}},
    "POST http://127.0.0.1:8080/api/login": {"code": 401, "body": {"ok": False}},
    "POST http://127.0.0.1:8080/api/chaos": {"body": {"ok": True}},
    "http://127.0.0.1:5001/health": {"body": {"status": "ok"}},
    "http://127.0.0.1:5001/api/alerts": {"body": {"alerts": [
        {"alertname": "HighErrorRate", "status": "firing"},
        {"alertname": "BruteForceLogin", "status": "firing"}]}},
    "http://127.0.0.1:3100/loki/api/v1/label/service/values": {"body": {"data": ["alert-receiver", "payments", "shop-api"]}},
    "http://127.0.0.1:3100/loki/api/v1/query": {"body": {"data": {"result": [
        {"metric": {"service": "payments"}, "value": [1, "12"]}]}}},
    "http://127.0.0.1:3100/loki/api/v1/query_range": {"body": {"data": {"result": [
        {"values": [["2", "level=ERROR service=payments event=payment_error trace_id=abc123"],
                    ["1", "level=ERROR service=shop-api event=checkout_failed trace_id=abc123"]]}]}}},
}


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("docker", FAKE_DOCKER), ("sudo", FAKE_SUDO)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps(GOOD_ROUTES))
    e = dict(os.environ)
    e.update({
        "PATH": f"{bindir}:{e['PATH']}",
        "FAKE_ROUTES": str(routes),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "ENV_FILE": str(tmp_path / ".env"),
        "SMOKE_TIMEOUT": "1", "SMOKE_SLEEP": "0", "WAIT_SECS": "1",
        "ALERT_TIMEOUT": "5", "SETTLE_SECS": "0",
    })
    e.pop("PUBLIC_IP_OVERRIDE", None)

    def set_routes(**changes):
        data = dict(GOOD_ROUTES)
        for k, v in changes.items():
            data[k.replace("__", " ")] = v
        routes.write_text(json.dumps(data))

    yield e, tmp_path, set_routes


def run(env_vars, script, *args, stdin=""):
    e = {k: v for k, v in env_vars.items() if isinstance(v, str)}
    return subprocess.run(["bash", str(ROOT / "scripts" / script), *args], env=e, cwd=ROOT,
                          capture_output=True, text=True, input=stdin, timeout=60)


def lib(env_vars, snippet):
    e = {k: v for k, v in env_vars.items() if isinstance(v, str)}
    return subprocess.run(["bash", "-c", f'source "{ROOT}/scripts/lib.sh"; {snippet}'], env=e,
                          capture_output=True, text=True, timeout=30)


# ------------------------------------------------------------------ public IP
def test_public_ip_from_imdsv2(env):
    e, _, _ = env
    r = lib(e, "get_public_ip")
    assert r.returncode == 0 and r.stdout.strip() == "54.1.2.3"


def test_public_ip_falls_back_to_checkip_when_imds_unreachable(env):
    e, _, set_routes = env
    set_routes(**{"PUT__http://169.254.169.254/latest/api/token": {"fail": True}})
    r = lib(e, "get_public_ip")
    assert r.stdout.strip() == "9.9.9.9"


def test_public_ip_falls_back_when_instance_has_no_public_ip(env):
    e, _, set_routes = env
    set_routes(**{"http://169.254.169.254/latest/meta-data/public-ipv4":
                  {"code": 404, "body": "<html>404 - Not Found</html>"}})
    assert lib(e, "get_public_ip").stdout.strip() == "9.9.9.9"


def test_public_ip_fails_cleanly_when_nothing_answers(env):
    e, _, set_routes = env
    set_routes(**{"PUT__http://169.254.169.254/latest/api/token": {"fail": True},
                  "https://checkip.amazonaws.com": {"fail": True}})
    assert lib(e, "get_public_ip").returncode == 1


def test_imds_token_header_is_sent(env):
    e, tmp, _ = env
    lib(e, "get_public_ip")
    calls = (tmp / "calls.log").read_text()
    assert "-X PUT http://169.254.169.254/latest/api/token" in calls
    assert "X-aws-ec2-metadata-token: TOKEN123" in calls


# ------------------------------------------------------------------ http_code / registry
def test_http_code_reports_401_and_000(env):
    e, _, _ = env
    assert lib(e, "http_code https://registry-1.docker.io/v2/").stdout == "401"
    assert lib(e, "http_code http://127.0.0.1:1/none").stdout == "000"


def test_preflight_registry_accepts_200_and_401_only(env):
    e, _, _ = env
    snippet = (f'source "{ROOT}/scripts/preflight.sh"; '
               'for c in 200 401 000 403 500; do registry_ok $c && echo "$c ok" || echo "$c bad"; done')
    out = subprocess.run(["bash", "-c", snippet], env={k: v for k, v in e.items() if isinstance(v, str)},
                         capture_output=True, text=True).stdout
    assert out.split("\n")[:5] == ["200 ok", "401 ok", "000 bad", "403 bad", "500 bad"]


# ------------------------------------------------------------------ .env handling
def test_ensure_env_creates_password_and_keeps_it(env):
    e, tmp, set_routes = env
    lib(e, "ensure_env")
    first = (tmp / ".env").read_text()
    assert "PUBLIC_IP=54.1.2.3" in first and "LOKI_IMAGE=grafana/loki:3.4.2" in first
    pw = [l for l in first.splitlines() if l.startswith("GRAFANA_ADMIN_PASSWORD=")][0]
    assert len(pw.split("=", 1)[1]) == 24
    assert oct((tmp / ".env").stat().st_mode)[-3:] == "600"
    # IP changes (instance stop/start): password stays, IP is refreshed
    set_routes(**{"http://169.254.169.254/latest/meta-data/public-ipv4": {"body": "3.3.3.3"}})
    lib(e, "ensure_env")
    second = (tmp / ".env").read_text()
    assert pw in second and "PUBLIC_IP=3.3.3.3" in second and "PUBLIC_IP=54.1.2.3" not in second


# ------------------------------------------------------------------ smoke.sh
def test_smoke_passes_with_healthy_stack(env):
    e, _, _ = env
    lib(e, "ensure_env")
    r = run(e, "smoke.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All 13 checks passed." in r.stdout


def test_smoke_fails_when_loki_not_ready(env):
    e, _, set_routes = env
    lib(e, "ensure_env")
    set_routes(**{"http://127.0.0.1:3100/ready": {"code": 503, "body": "Ingester not ready"}})
    r = run(e, "smoke.sh")
    assert r.returncode == 1
    assert "1 of 13 checks failed." in r.stdout


def test_smoke_detects_stopped_container(env):
    e, _, _ = env
    lib(e, "ensure_env")
    e["FAKE_RUNNING"] = "loki grafana shop-api payments alert-receiver"
    r = run(e, "smoke.sh")
    assert r.returncode == 1 and "not running: promtail" in r.stdout


def test_smoke_fails_when_marker_never_reaches_loki(env):
    e, _, set_routes = env
    lib(e, "ensure_env")
    set_routes(**{"http://127.0.0.1:3100/loki/api/v1/query": {"body": {"data": {"result": []}}}})
    r = run(e, "smoke.sh")
    assert r.returncode == 1 and "marker" in r.stdout


def test_smoke_requires_env(env):
    e, _, _ = env
    r = run(e, "smoke.sh")
    assert r.returncode == 1 and "GRAFANA_ADMIN_PASSWORD" in r.stderr


# ------------------------------------------------------------------ up / demo / destroy
def test_up_starts_stack_and_prints_public_urls(env):
    e, tmp, _ = env
    r = run(e, "up.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "http://54.1.2.3:3000/d/logging-overview" in r.stdout
    assert "http://54.1.2.3:8080/" in r.stdout and "http://54.1.2.3:5001/" in r.stdout
    # stub says the app image exists -> no rebuild (no Docker Hub dependency at demo time)
    assert "docker compose up -d --remove-orphans" in (tmp / "calls.log").read_text()


def test_up_builds_when_app_image_missing(env):
    e, tmp, _ = env
    e["FAKE_NO_IMAGE"] = "1"
    r = run(e, "up.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker compose up -d --build --remove-orphans" in (tmp / "calls.log").read_text()


def test_up_fails_when_a_service_never_gets_ready(env):
    e, _, set_routes = env
    set_routes(**{"http://127.0.0.1:3000/api/health": {"fail": True}})
    r = run(e, "up.sh")
    assert r.returncode == 1 and "Grafana not ready" in r.stdout


def test_demo_auto_runs_all_steps(env):
    e, tmp, _ = env
    lib(e, "ensure_env")
    r = run(e, "demo.sh", "--auto")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "HighErrorRate is FIRING" in r.stdout and "BruteForceLogin is FIRING" in r.stdout
    assert "Following trace_id=abc123" in r.stdout
    assert "payments: 12 ERROR lines" in r.stdout
    calls = (tmp / "calls.log").read_text()
    assert '"target":"payments","mode":"errors"' in calls
    assert '"target":"shop-api","mode":"bruteforce"' in calls


def test_demo_warns_when_alert_does_not_arrive(env):
    e, _, set_routes = env
    lib(e, "ensure_env")
    set_routes(**{"http://127.0.0.1:5001/api/alerts": {"body": {"alerts": []}}})
    r = run(e, "demo.sh", "--auto")
    assert "did not arrive" in r.stdout and "Demo finished." in r.stdout


def test_destroy_asks_and_can_be_cancelled(env):
    e, tmp, _ = env
    r = run(e, "destroy.sh", stdin="n\n")
    assert "Cancelled." in r.stdout
    assert "compose down" not in (tmp / "calls.log").read_text() if (tmp / "calls.log").exists() else True


def test_destroy_yes_removes_volumes(env):
    e, tmp, _ = env
    lib(e, "ensure_env")
    r = run(e, "destroy.sh", "-y")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "compose down -v --remove-orphans" in (tmp / "calls.log").read_text()
    assert (tmp / ".env").exists()


def test_destroy_all_removes_images_and_env(env):
    e, tmp, _ = env
    lib(e, "ensure_env")
    r = run(e, "destroy.sh", "--all", "-y")
    assert r.returncode == 0
    assert "--rmi all" in (tmp / "calls.log").read_text()
    assert not (tmp / ".env").exists()


def test_destroy_rejects_unknown_option(env):
    e, _, _ = env
    assert run(e, "destroy.sh", "--force").returncode == 1
