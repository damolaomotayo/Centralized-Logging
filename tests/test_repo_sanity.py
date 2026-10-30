"""Repo sanity: required files, executable scripts, bash syntax, compose rules, docs."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))

REQUIRED = [
    "README.md", "STEPS.md", "DEMO.md", "DIAGRAM_PROMPT.md", ".gitignore", ".gitattributes",
    ".env.example", "docker-compose.yml", "app/app.py", "app/Dockerfile", "loki/loki.yaml",
    "promtail/promtail.yaml", "grafana/dashboards/logging-overview.json",
    "scripts/setup.sh", "scripts/preflight.sh", "scripts/up.sh", "scripts/smoke.sh",
    "scripts/destroy.sh", "scripts/demo.sh", "scripts/test.sh", "scripts/lib.sh",
]
PUBLIC_PORTS = {"3000", "8080", "5001"}


def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


@pytest.mark.parametrize("rel", REQUIRED)
def test_required_file_exists(rel):
    assert (ROOT / rel).is_file(), rel


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_executable_lf_and_valid_bash(script):
    assert os.access(script, os.X_OK), f"chmod +x {script.name}"
    data = script.read_bytes()
    assert b"\r\n" not in data, "CRLF line endings"
    assert data.startswith(b"#!/usr/bin/env bash")
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_never_uses_curl_fail_flag(script):
    # -f hides non-2xx codes (Docker Hub /v2/ answers 401 by design). Only allowed for the
    # Docker apt key download in setup.sh, where 200 is the only valid answer.
    for line in script.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"\bcurl\b.*\s-[a-zA-Z]*f[a-zA-Z]*\b", line) and "download.docker.com" not in line:
            pytest.fail(f"{script.name}: curl -f used: {line.strip()}")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    subprocess.run(["shellcheck", "-x", "-P", str(ROOT / "scripts"), *map(str, SCRIPTS)],
                   check=True, cwd=ROOT)


def test_every_service_has_memory_limit_and_pinned_image():
    c = compose()
    for name, svc in c["services"].items():
        assert "mem_limit" in svc, f"{name} has no mem_limit"
        image = svc["image"]
        assert not image.endswith(":latest") and ":" in image, f"{name} image not pinned"


def test_total_memory_limits_fit_8gib_with_headroom():
    units = {"m": 1, "g": 1024}
    total = sum(int(s["mem_limit"][:-1]) * units[s["mem_limit"][-1]]
                for s in compose()["services"].values())
    assert total <= 4096, f"limits add up to {total} MiB"


def test_ports_public_only_where_intended():
    for name, svc in compose()["services"].items():
        for p in svc.get("ports", []):
            host_ip, host_port, _ = p.split(":")
            if host_port in PUBLIC_PORTS:
                assert host_ip == "0.0.0.0", f"{name}:{p}"
            else:
                assert host_ip == "127.0.0.1", f"{name}:{p} must stay on the instance"


def test_no_minio_or_localstack_and_no_docker_socket():
    text = (ROOT / "docker-compose.yml").read_text().lower()
    assert "minio" not in text and "localstack" not in text
    assert "docker.sock" not in text


def test_steps_lists_a_security_group_rule_per_public_port():
    steps = (ROOT / "STEPS.md").read_text()
    for port in PUBLIC_PORTS | {"22"}:
        assert re.search(rf"\|\s*(Custom TCP|SSH)\s*\|\s*{port}\s*\|\s*My IP", steps), port
    assert "0.0.0.0/0" not in steps.replace("Never 0.0.0.0/0", "")
    for s in SCRIPTS:
        if s.name != "lib.sh":
            assert f"scripts/{s.name}" in steps, f"STEPS.md does not mention {s.name}"


def test_steps_prerequisites_come_first():
    steps = (ROOT / "STEPS.md").read_text()
    first_h2 = re.search(r"^## (.+)$", steps, re.M).group(1)
    assert "Prerequisites" in first_h2


def test_smoke_ends_with_all_n_checks_passed():
    assert "All %d checks passed." in (ROOT / "scripts/smoke.sh").read_text()


def test_gitattributes_forces_lf_for_scripts():
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text()


def test_env_is_ignored():
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()


def test_demo_md_is_short():
    words = len((ROOT / "DEMO.md").read_text().split())
    assert words < 450, f"DEMO.md has {words} words; keep it under one page"
