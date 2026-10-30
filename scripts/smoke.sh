#!/usr/bin/env bash
# Smoke test of the running stack. Talks to the services on 127.0.0.1 (the
# instance itself), because the security group only lets YOUR laptop reach the
# public IP. Ends with "All N checks passed." or exits 1.
set -uo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-60}"   # seconds to wait for logs to arrive in Loki
SMOKE_SLEEP="${SMOKE_SLEEP:-3}"
PASS=0
FAILED=0

check() {  # check "<description>" <command...>
  local desc="$1"; shift
  if "$@"; then ok "$desc"; PASS=$((PASS + 1)); else fail "$desc"; FAILED=$((FAILED + 1)); fi
}

retry() {  # retry <command...> until success or SMOKE_TIMEOUT
  local start
  start=$(date +%s)
  until "$@"; do
    [ $(( $(date +%s) - start )) -ge "$SMOKE_TIMEOUT" ] && return 1
    sleep "$SMOKE_SLEEP"
  done
}

gf() {  # authenticated Grafana API GET -> body
  curl -s -m 10 -u "admin:$GF_PW" "$GRAFANA_URL$1" 2>/dev/null || true
}

# --- individual checks ----------------------------------------------------
all_containers_running() {
  local want have s missing=0
  want="$(compose config --services 2>/dev/null)"
  have="$(compose ps --services --status running 2>/dev/null)"
  [ -n "$want" ] || return 1
  for s in $want; do
    if ! grep -qx "$s" <<<"$have"; then echo "      not running: $s"; missing=1; fi
  done
  [ "$missing" -eq 0 ]
}
code_is() { [ "$(http_code "$1" 5)" = "$2" ]; }
datasource_ok() { [ "$(gf /api/datasources/uid/loki/health | jq -r '.status // empty' 2>/dev/null)" = "OK" ]; }
dashboard_ok() { [ "$(gf /api/dashboards/uid/logging-overview | jq -r '.dashboard.uid // empty' 2>/dev/null)" = "logging-overview" ]; }
alert_rules_ok() { [ "$(gf /api/v1/provisioning/alert-rules | jq 'length' 2>/dev/null)" = "3" ]; }
contact_point_ok() {
  [ "$(gf /api/v1/provisioning/contact-points | jq '[.[] | select(.name=="demo-webhook")] | length' 2>/dev/null)" -ge 1 ] 2>/dev/null
}
payments_ok() {
  [ "$(curl -s -m 5 "$SHOP_URL/api/status" 2>/dev/null | jq -r '.payments.ok // false' 2>/dev/null)" = "true" ]
}
loki_services_ok() {
  local vals
  vals="$(curl -s -m 5 "$LOKI_URL/loki/api/v1/label/service/values" 2>/dev/null | jq -r '.data[]?' 2>/dev/null)"
  grep -qx "shop-api" <<<"$vals" && grep -qx "payments" <<<"$vals"
}
marker_in_loki() {
  local q="sum(count_over_time({job=\"apps\", service=\"shop-api\"} |= \"user=$MARKER\" [10m]))"
  [ "$(loki_value "$q")" -ge 1 ] 2>/dev/null
}

main() {
  pick_docker || die "Cannot talk to Docker (try: newgrp docker)"
  GF_PW="$(env_get GRAFANA_ADMIN_PASSWORD)"
  [ -n "$GF_PW" ] || die ".env has no GRAFANA_ADMIN_PASSWORD - run scripts/up.sh first"

  info "Containers"
  check "all compose services are running"              all_containers_running

  info "Logging pipeline"
  check "Loki /ready returns 200"                       retry code_is "$LOKI_URL/ready" 200
  check "Promtail /ready returns 200"                   retry code_is "$PROMTAIL_URL/ready" 200
  check "Grafana /api/health returns 200"               retry code_is "$GRAFANA_URL/api/health" 200
  check "Grafana -> Loki datasource health is OK"       retry datasource_ok
  check "dashboard 'Logging Overview' is provisioned"   retry dashboard_ok
  check "3 log-based alert rules are provisioned"       retry alert_rules_ok
  check "webhook contact point 'demo-webhook' exists"   contact_point_ok

  info "Demo apps"
  check "shop-api /health returns 200"                  code_is "$SHOP_URL/health" 200
  check "payments reachable from shop-api"              payments_ok
  check "alert-receiver /health returns 200"            code_is "$ALERTS_URL/health" 200

  info "End to end: app -> log file -> Promtail -> Loki"
  check "Loki has logs labelled service=shop-api and payments" retry loki_services_ok
  MARKER="smoke-$(openssl rand -hex 4)"
  curl -s -m 5 -o /dev/null -X POST "$SHOP_URL/api/login" -H 'Content-Type: application/json' \
       -d "{\"user\":\"$MARKER\",\"password\":\"x\"}" || true
  check "a fresh marker log line ($MARKER) is searchable in Loki" retry marker_in_loki

  echo
  local total=$((PASS + FAILED))
  if [ "$FAILED" -eq 0 ]; then
    printf '%sAll %d checks passed.%s\n' "$C_OK" "$total" "$C_0"
  else
    printf '%s%d of %d checks failed.%s See STEPS.md troubleshooting.\n' "$C_BAD" "$FAILED" "$total" "$C_0"
    exit 1
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
