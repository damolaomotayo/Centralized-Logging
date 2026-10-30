#!/usr/bin/env bash
# Start the stack, wait until it is healthy, print the browser URLs.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WAIT_SECS="${WAIT_SECS:-180}"

wait_for() {  # wait_for <name> <url> <expected-code>
  local name="$1" url="$2" want="$3" start code
  start=$(date +%s)
  while :; do
    code="$(http_code "$url" 3)"
    if [ "$code" = "$want" ]; then ok "$name is up"; return 0; fi
    if [ $(( $(date +%s) - start )) -ge "$WAIT_SECS" ]; then
      fail "$name not ready after ${WAIT_SECS}s (last HTTP $code)"; return 1
    fi
    sleep 3
  done
}

pick_docker || die "Cannot talk to Docker. Run scripts/setup.sh, then 'newgrp docker'."

info "Preparing .env (refreshes the public IP in case it changed)"
ensure_env
IP="$(env_get PUBLIC_IP)"
ok "public IP: $IP"

info "Starting containers"
# Build only if the app image is missing: a rebuild makes BuildKit re-check the base
# image on Docker Hub, which we don't want to depend on during a live demo.
if dk image inspect logging-demo-app:1.0 >/dev/null 2>&1; then
  compose up -d --remove-orphans
else
  compose up -d --build --remove-orphans
fi

info "Waiting for services (Loki takes ~20s to report ready)"
rc=0
wait_for "Loki"           "$LOKI_URL/ready"         200 || rc=1
wait_for "Promtail"       "$PROMTAIL_URL/ready"     200 || rc=1
wait_for "Grafana"        "$GRAFANA_URL/api/health" 200 || rc=1
wait_for "Shop API"       "$SHOP_URL/health"        200 || rc=1
wait_for "Alert receiver" "$ALERTS_URL/health"      200 || rc=1

compose ps
if [ "$rc" -ne 0 ]; then
  echo; die "Some services did not start. Check: docker compose logs --tail=50 <service>"
fi

print_urls "$IP"
echo
echo "Next: ./scripts/smoke.sh   (then ./scripts/demo.sh)"
