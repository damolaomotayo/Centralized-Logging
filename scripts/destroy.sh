#!/usr/bin/env bash
# Stop and remove the stack.
#   ./scripts/destroy.sh          containers + volumes (all stored logs, Grafana state)
#   ./scripts/destroy.sh --all    also images and .env
#   add -y to skip the question
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ALL=0; YES=0
for arg in "$@"; do
  case "$arg" in
    --all) ALL=1 ;;
    -y|--yes) YES=1 ;;
    *) die "unknown option: $arg (use --all and/or -y)" ;;
  esac
done

if [ "$YES" -ne 1 ]; then
  read -r -p "Delete the logging stack and all its data? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

pick_docker || die "Cannot talk to Docker (try: newgrp docker)"
# compose needs the variables from .env to parse the file; give safe dummies if it is gone.
[ -f "$ENV_FILE" ] || export GRAFANA_ADMIN_PASSWORD=unused

info "Removing containers, network and volumes"
if [ "$ALL" -eq 1 ]; then
  compose down -v --remove-orphans --rmi all
  rm -f "$ENV_FILE"
  ok "images and .env removed"
else
  compose down -v --remove-orphans
fi
ok "Stack destroyed."
echo "Reminder: in the AWS console, terminate the EC2 instance and delete its security group."
