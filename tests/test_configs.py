"""Static checks of Loki, Promtail and Grafana configuration (no Docker needed)."""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROV = ROOT / "grafana" / "provisioning"


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


def test_loki_single_node_filesystem_tsdb_v13():
    cfg = load("loki/loki.yaml")
    assert cfg["auth_enabled"] is False
    assert cfg["server"]["http_listen_port"] == 3100
    schema = cfg["schema_config"]["configs"][0]
    assert schema["store"] == "tsdb" and schema["schema"] == "v13"
    assert schema["object_store"] == "filesystem"
    assert cfg["common"]["replication_factor"] == 1
    assert cfg["compactor"]["retention_enabled"] is True
    assert cfg["limits_config"]["retention_period"] == "72h"


def test_promtail_ships_to_loki_with_low_cardinality_labels():
    cfg = load("promtail/promtail.yaml")
    assert cfg["clients"][0]["url"] == "http://loki:3100/loki/api/v1/push"
    jobs = {s["job_name"]: s for s in cfg["scrape_configs"]}
    assert set(jobs) == {"apps", "host"}
    apps = jobs["apps"]
    assert apps["static_configs"][0]["labels"]["__path__"] == "/var/log/app/*.log"
    label_stage = next(s["labels"] for s in apps["pipeline_stages"] if "labels" in s)
    assert set(label_stage) == {"level", "service"}, "high-cardinality fields must not be labels"
    paths = [c["labels"]["__path__"] for c in jobs["host"]["static_configs"]]
    assert "/var/log/host/auth.log" in paths


def test_no_unescaped_dollar_in_provisioning():
    # Grafana expands $VAR / ${VAR} in provisioning YAML; literal $ must be written $$.
    for f in PROV.rglob("*.yaml"):
        lines = [ln for ln in f.read_text().splitlines() if not ln.lstrip().startswith("#")]
        text = re.sub(r"\$\$", "", "\n".join(lines))
        assert "$" not in text, f"unescaped $ in {f.relative_to(ROOT)}"


def test_datasource_and_derived_trace_link():
    ds = load("grafana/provisioning/datasources/loki.yaml")["datasources"][0]
    assert ds["uid"] == "loki" and ds["url"] == "http://loki:3100" and ds["isDefault"]
    field = ds["jsonData"]["derivedFields"][0]
    assert field["datasourceUid"] == "loki"
    assert re.search(field["matcherRegex"], "level=ERROR trace_id=abc123def status=500").group(1) == "abc123def"


def test_alert_rules_are_well_formed():
    groups = load("grafana/provisioning/alerting/rules.yaml")["groups"]
    rules = groups[0]["rules"]
    assert {r["title"] for r in rules} == {"HighErrorRate", "BruteForceLogin", "SlowCheckout"}
    assert len({r["uid"] for r in rules}) == len(rules)
    for r in rules:
        refs = {d["refId"]: d for d in r["data"]}
        assert r["condition"] in refs
        assert refs["A"]["datasourceUid"] == "loki"
        assert refs["A"]["model"]["expr"].count("(") == refs["A"]["model"]["expr"].count(")")
        assert refs["B"]["model"]["expression"] == "A"
        assert refs["C"]["model"]["expression"] == "B"
        assert refs["C"]["model"]["conditions"][0]["evaluator"]["type"] == "gt"
        assert r["annotations"]["summary"]


def test_alerts_route_to_webhook_receiver():
    cps = load("grafana/provisioning/alerting/contact-points.yaml")["contactPoints"]
    pol = load("grafana/provisioning/alerting/policies.yaml")["policies"][0]
    names = {c["name"] for c in cps}
    assert pol["receiver"] in names
    recv = cps[0]["receivers"][0]
    assert recv["type"] == "webhook"
    assert recv["settings"]["url"] == "http://alert-receiver:5001/alert"


def test_dashboard_json():
    dash = json.loads((ROOT / "grafana/dashboards/logging-overview.json").read_text())
    assert dash["uid"] == "logging-overview"
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    for p in dash["panels"]:
        assert p["datasource"]["uid"] == "loki"
        for t in p["targets"]:
            expr = t["expr"]
            assert expr.count("(") == expr.count(")"), p["title"]
            assert expr.count("{") == expr.count("}"), p["title"]
            assert expr.count('"') % 2 == 0, p["title"]
    types = {p["type"] for p in dash["panels"]}
    assert {"stat", "timeseries", "logs"} <= types
    var_names = {v["name"] for v in dash["templating"]["list"]}
    assert var_names == {"service", "search"}


def test_dashboard_provider_points_at_mounted_folder():
    prov = load("grafana/provisioning/dashboards/provider.yaml")["providers"][0]
    compose = load("docker-compose.yml")
    mounts = compose["services"]["grafana"]["volumes"]
    assert any(m.endswith(":" + prov["options"]["path"] + ":ro") for m in mounts)
