#!/usr/bin/env bash
# One-time instance setup on Ubuntu 24.04: OS packages, Docker Engine + Compose
# plugin, test tools, and the .env file (random Grafana password, public IP).
# Safe to re-run.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ "$(id -u)" -ne 0 ] || die "Run as the normal 'ubuntu' user (the script uses sudo where needed)."

info "Updating the OS (apt update && apt upgrade)"
sudo apt-get update -y
sudo NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

info "Installing base packages"
sudo NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl jq git unzip openssl \
  python3 python3-pytest python3-yaml

install_docker_official() {
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  local codename arch
  codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
  arch="$(dpkg --print-architecture)"
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_docker_ubuntu() {
  # Fallback: Ubuntu's own packages (older, but from the Ubuntu mirror).
  sudo rm -f /etc/apt/sources.list.d/docker.list
  sudo apt-get update -y
  sudo NEEDRESTART_MODE=a DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 docker-buildx
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  info "Docker and the Compose plugin are already installed"
else
  info "Installing Docker Engine + Compose plugin (Docker's apt repository)"
  if ! install_docker_official; then
    warn "Docker's repository failed; falling back to Ubuntu's docker.io packages"
    install_docker_ubuntu
  fi
fi

sudo systemctl enable --now docker
if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  NEED_RELOGIN=1
fi

info "Making scripts executable"
chmod +x "$REPO_ROOT"/scripts/*.sh

info "Creating .env (random Grafana admin password, public IP)"
ensure_env
ok ".env ready (mode 600)"

info "Versions"
docker --version 2>/dev/null || sudo docker --version
docker compose version 2>/dev/null || sudo docker compose version
python3 --version
jq --version

echo
if [ "${NEED_RELOGIN:-0}" = "1" ]; then
  printf '%sIMPORTANT:%s you were added to the docker group. Run:  newgrp docker\n' "$C_WARN" "$C_0"
  echo "(or log out and SSH back in). The scripts also fall back to sudo docker."
fi
echo "Setup done. Next: ./scripts/preflight.sh"
