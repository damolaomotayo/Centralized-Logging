#!/usr/bin/env bash
# Scripted demo. Pauses between steps (press Enter) unless --auto is given.
#   1. baseline   2. payments outage -> HighErrorRate alert
#   3. follow one failing request by trace_id across services
#   4. brute-force login -> BruteForceLogin alert   5. back to normal
set -uo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

AUTO=0
ALERT_TIMEOUT="${ALERT_TIMEOUT:-180}"
SETTLE_SECS="${SETTLE_SECS:-15}"
[ "${1:-}" = "--auto" ] && AUTO=1

pause() {
  if [ "$AUTO" -eq 1 ]; then sleep 2; else read -r -p "  (press Enter to continue) " _; fi
}

chaos() {  # chaos <target> <mode> [seconds]
  curl -s -m 5 -X POST "$SHOP_URL/api/chaos" -H 'Content-Type: application/json' \
    -d "{\"target\":\"$1\",\"mode\":\"$2\",\"seconds\":${3:-90}}"
  echo
}

errors_by_service() {
  local out
  out="$(curl -s -m 10 -G "$LOKI_URL/loki/api/v1/query" \
    --data-urlencode 'query=sum by (service) (count_over_time({job="apps", level="ERROR"}[1m]))' 2>/dev/null \
    | jq -r '.data.result[]? | "    \(.metric.service): \(.value[1]) ERROR lines in the last minute"' 2>/dev/null)"
  if [ -n "$out" ]; then echo "$out"; else echo "    0 ERROR lines in the last minute"; fi
}

alert_firing() {  # alert_firing <alertname>
  curl -s -m 5 "$ALERTS_URL/api/alerts" 2>/dev/null \
    | jq -e --arg n "$1" '[.alerts[]? | select(.alertname==$n and .status=="firing")] | length > 0' >/dev/null 2>&1
}

wait_alert() {  # wait_alert <alertname>
  local name="$1" start
  start=$(date +%s)
  printf '  waiting for %s to fire (usually 40-90 s)' "$name"
  until alert_firing "$name"; do
    if [ $(( $(date +%s) - start )) -ge "$ALERT_TIMEOUT" ]; then
      echo; warn "$name did not arrive within ${ALERT_TIMEOUT}s - check Grafana > Alerting > Alert rules"
      return 1
    fi
    printf '.'; sleep 5
  done
  echo; ok "$name is FIRING - it is in the alert inbox ($(( $(date +%s) - start ))s)"
}

show_trace() {
  local tid start_ns
  start_ns="$(( ($(date +%s) - 300) * 1000000000 ))"
  tid="$(curl -s -m 10 -G "$LOKI_URL/loki/api/v1/query_range" \
      --data-urlencode 'query={job="apps", service="shop-api", level="ERROR"} |= "event=checkout_failed"' \
      --data-urlencode "start=$start_ns" --data-urlencode 'limit=1' 2>/dev/null \
    | jq -r '.data.result[0].values[0][1] // empty' 2>/dev/null \
    | grep -oE 'trace_id=[0-9a-f]+' | head -n1 | cut -d= -f2 || true)"
  if [ -z "$tid" ]; then warn "no failed checkout found yet"; return 0; fi
  echo "  Following trace_id=$tid across services:"
  curl -s -m 10 -G "$LOKI_URL/loki/api/v1/query_range" \
      --data-urlencode "query={job=\"apps\"} |= \"trace_id=$tid\"" \
      --data-urlencode "start=$start_ns" --data-urlencode 'direction=forward' 2>/dev/null \
    | jq -r '[.data.result[]? | .values[]? ] | sort_by(.[0]) | .[] | "    " + .[1]' 2>/dev/null
  echo "  In Grafana: open a log line, click the trace_id link -> same view."
}

main() {
  pick_docker || die "Cannot talk to Docker (try: newgrp docker)"
  local ip
  ip="$(env_get PUBLIC_IP)"
  print_urls "${ip:-<public-ip>}"

  info "Step 1/5 - Baseline: the shop is generating normal traffic"
  errors_by_service
  echo "  Show: Grafana dashboard (log volume by service, near-zero errors)."
  pause

  info "Step 2/5 - Break the payments service for 90 s"
  chaos payments errors 90
  sleep "$SETTLE_SECS"
  errors_by_service
  wait_alert HighErrorRate
  echo "  Show: dashboard error panels climbing, and the alert inbox page."
  pause

  info "Step 3/5 - Follow one failing request through both services"
  show_trace
  pause

  info "Step 4/5 - Simulate a brute-force login attack for 60 s"
  chaos shop-api bruteforce 60
  wait_alert BruteForceLogin
  echo "  Show: 'Failed logins by source IP' panel - one IP (203.0.113.66) spikes."
  pause

  info "Step 5/5 - Back to normal"
  chaos payments normal
  chaos shop-api normal
  echo "  Alerts resolve by themselves in ~1-2 minutes; the inbox shows 'resolved'."
  echo
  ok "Demo finished."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
