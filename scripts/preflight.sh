#!/usr/bin/env bash
# Run BEFORE the demo: checks the host, pulls every image and builds the app image,
# so nothing has to be downloaded while people are watching.
set -uo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

FAILS=0
check_fail() { fail "$*"; FAILS=$((FAILS + 1)); }

# Docker Hub's registry answers 401 (auth required) when it is reachable and healthy.
registry_ok() {
  case "$1" in
    200|401) return 0 ;;
    *) return 1 ;;
  esac
}

pull_with_retry() {
  local image="$1" i
  for i in 1 2 3; do
    if dk pull -q "$image" >/dev/null; then
      ok "pulled $image"; return 0
    fi
    warn "pull of $image failed (attempt $i/3), retrying in 5s"
    sleep 5
  done
  check_fail "could not pull $image - see STEPS.md troubleshooting (image fallbacks)"
  return 1
}

port_free() {
  ! ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
}

main() {
  info "Host checks"
  for cmd in docker curl jq python3 openssl ss; do
    if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd found"; else check_fail "$cmd missing (run scripts/setup.sh)"; fi
  done
  if python3 -c "import pytest, yaml" 2>/dev/null; then ok "python3 pytest + yaml"; else check_fail "python3-pytest / python3-yaml missing (run scripts/setup.sh)"; fi

  if pick_docker; then
    ok "Docker daemon reachable (${DOCKER[*]})"
  else
    check_fail "cannot talk to Docker (is it installed and running? try: newgrp docker)"
    echo; die "Preflight stopped: Docker is required for the rest."
  fi
  if dk compose version >/dev/null 2>&1; then ok "$(dk compose version | head -n1)"; else check_fail "Docker Compose plugin missing"; fi

  local mem_mb disk_gb
  mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
  if [ "$mem_mb" -ge 3500 ]; then ok "memory ${mem_mb} MiB (stack needs ~2 GiB)"; else check_fail "only ${mem_mb} MiB RAM; need at least 4 GiB"; fi
  disk_gb="$(df -Pk "$REPO_ROOT" | awk 'NR==2 {print int($4/1024/1024)}')"
  if [ "$disk_gb" -ge 5 ]; then ok "free disk ${disk_gb} GiB"; else check_fail "only ${disk_gb} GiB free disk; need 5+"; fi

  info "Network checks"
  local code
  code="$(http_code https://registry-1.docker.io/v2/ 10)"
  if registry_ok "$code"; then ok "Docker Hub registry reachable (HTTP $code, 401 is expected)"; else check_fail "Docker Hub registry not reachable (HTTP $code)"; fi
  local ip
  if ip="$(get_public_ip)"; then ok "public IP $ip"; else warn "public IP not detected (URLs will show localhost)"; fi

  ensure_env

  info "Ports (must be free before up.sh, unless the stack is already running)"
  local running=""
  running="$(compose ps --services --status running 2>/dev/null || true)"
  for p in "$GRAFANA_PORT" "$SHOP_PORT" "$ALERTS_PORT" "$LOKI_PORT" "$PROMTAIL_PORT"; do
    if port_free "$p"; then ok "port $p free"
    elif [ -n "$running" ]; then ok "port $p in use by the running stack"
    else check_fail "port $p already in use by something else"; fi
  done

  info "Config and images"
  if compose config -q; then ok "docker-compose.yml is valid"; else check_fail "docker-compose.yml invalid"; fi
  local img
  for key in LOKI_IMAGE PROMTAIL_IMAGE GRAFANA_IMAGE PYTHON_IMAGE; do
    img="$(env_get "$key")"
    pull_with_retry "$img" || true
  done
  if compose build -q; then ok "built logging-demo-app:1.0"; else check_fail "app image build failed"; fi

  echo
  if [ "$FAILS" -eq 0 ]; then
    printf '%sPreflight passed.%s Next: ./scripts/test.sh then ./scripts/up.sh\n' "$C_OK" "$C_0"
  else
    printf '%sPreflight found %d problem(s).%s Fix them before the demo.\n' "$C_BAD" "$FAILS" "$C_0"
    exit 1
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
