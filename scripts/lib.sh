#!/usr/bin/env bash
# Shared helpers. Sourced by the other scripts; not run directly.
# shellcheck disable=SC2034  # variables here are used by the scripts that source lib.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

# Ports
GRAFANA_PORT=3000
SHOP_PORT=8080
ALERTS_PORT=5001
LOKI_PORT=3100
PROMTAIL_PORT=9080

# Endpoints used from the instance itself (scripts never need the public IP to talk to services).
LOKI_URL="${LOKI_URL:-http://127.0.0.1:$LOKI_PORT}"
PROMTAIL_URL="${PROMTAIL_URL:-http://127.0.0.1:$PROMTAIL_PORT}"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:$GRAFANA_PORT}"
SHOP_URL="${SHOP_URL:-http://127.0.0.1:$SHOP_PORT}"
ALERTS_URL="${ALERTS_URL:-http://127.0.0.1:$ALERTS_PORT}"
IMDS_URL="${IMDS_URL:-http://169.254.169.254}"
CHECKIP_URL="${CHECKIP_URL:-https://checkip.amazonaws.com}"

# Images (override in .env if Docker Hub has trouble - see STEPS.md troubleshooting)
DEFAULT_LOKI_IMAGE="grafana/loki:3.4.2"
DEFAULT_PROMTAIL_IMAGE="grafana/promtail:3.4.2"
DEFAULT_GRAFANA_IMAGE="grafana/grafana:11.6.1"
DEFAULT_PYTHON_IMAGE="python:3.12-slim"

if [ -t 1 ]; then
  C_OK=$'\e[32m'; C_BAD=$'\e[31m'; C_WARN=$'\e[33m'; C_B=$'\e[1m'; C_0=$'\e[0m'
else
  C_OK=""; C_BAD=""; C_WARN=""; C_B=""; C_0=""
fi

info() { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
ok()   { printf '  %s[ OK ]%s %s\n' "$C_OK" "$C_0" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$C_WARN" "$C_0" "$*"; }
fail() { printf '  %s[FAIL]%s %s\n' "$C_BAD" "$C_0" "$*"; }
die()  { printf '%sERROR:%s %s\n' "$C_BAD" "$C_0" "$*" >&2; exit 1; }

# HTTP status code of a URL, "000" if unreachable. Never uses curl -f, so
# endpoints that answer 401/404 by design are reported, not treated as errors.
http_code() {
  local url="$1" timeout="${2:-5}"
  curl -s -o /dev/null -w '%{http_code}' -m "$timeout" "$url" 2>/dev/null || true
}

is_ipv4() {
  [[ "${1:-}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
}

# Public IPv4 of this instance: $PUBLIC_IP override -> IMDSv2 -> checkip.amazonaws.com
get_public_ip() {
  local token="" ip=""
  if is_ipv4 "${PUBLIC_IP_OVERRIDE:-}"; then
    printf '%s\n' "$PUBLIC_IP_OVERRIDE"; return 0
  fi
  token="$(curl -s -m 2 -X PUT "$IMDS_URL/latest/api/token" \
            -H "X-aws-ec2-metadata-token-ttl-seconds: 300" 2>/dev/null || true)"
  if [ -n "$token" ] && [[ "$token" != *"<"* ]]; then
    ip="$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $token" \
            "$IMDS_URL/latest/meta-data/public-ipv4" 2>/dev/null || true)"
  fi
  if ! is_ipv4 "$ip"; then
    ip="$(curl -s -m 5 "$CHECKIP_URL" 2>/dev/null | tr -d '[:space:]' || true)"
  fi
  is_ipv4 "$ip" || return 1
  printf '%s\n' "$ip"
}

# Read KEY from .env (empty if missing)
env_get() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true
}

# Set KEY=VALUE in .env (replaces an existing line, keeps the others)
env_set() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ]; then
    grep -vE "^${key}=" "$ENV_FILE" > "$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

# Create .env with a random Grafana password if it does not exist yet; refresh PUBLIC_IP.
ensure_env() {
  if [ -z "$(env_get GRAFANA_ADMIN_PASSWORD)" ]; then
    env_set GRAFANA_ADMIN_PASSWORD "$(openssl rand -hex 12)"
  fi
  local ip
  if ip="$(get_public_ip)"; then
    env_set PUBLIC_IP "$ip"
  elif [ -z "$(env_get PUBLIC_IP)" ]; then
    warn "Could not detect the public IP; using localhost in Grafana's ROOT_URL"
    env_set PUBLIC_IP "localhost"
  fi
  local k
  for k in LOKI_IMAGE PROMTAIL_IMAGE GRAFANA_IMAGE PYTHON_IMAGE; do
    if [ -z "$(env_get "$k")" ]; then
      local def="DEFAULT_$k"
      env_set "$k" "${!def}"
    fi
  done
}

# Docker command: plain "docker", or "sudo docker" right after setup.sh when the
# docker group is not active in this shell yet.
DOCKER=(docker)
pick_docker() {
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
  else
    return 1
  fi
}
dk() { "${DOCKER[@]}" "$@"; }
compose() { (cd "$REPO_ROOT" && "${DOCKER[@]}" compose "$@"); }

print_urls() {
  local ip="$1"
  printf '\n%sOpen these in your browser:%s\n' "$C_B" "$C_0"
  printf '  Grafana dashboard : http://%s:%s/d/logging-overview\n' "$ip" "$GRAFANA_PORT"
  printf '  Grafana Explore   : http://%s:%s/explore\n' "$ip" "$GRAFANA_PORT"
  printf '  Shop control panel: http://%s:%s/\n' "$ip" "$SHOP_PORT"
  printf '  Alert inbox       : http://%s:%s/\n' "$ip" "$ALERTS_PORT"
  printf '  Grafana login     : admin / %s   (stored in .env)\n' "$(env_get GRAFANA_ADMIN_PASSWORD)"
}

# Loki instant query -> prints the summed numeric value (0 if no data)
loki_value() {
  local query="$1"
  curl -s -m 10 -G "$LOKI_URL/loki/api/v1/query" --data-urlencode "query=$query" 2>/dev/null \
    | jq -r '[.data.result[]?.value[1] | tonumber] | add // 0' 2>/dev/null || echo 0
}
