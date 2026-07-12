#!/usr/bin/env bash
# One-shot install for a fresh Ubuntu DigitalOcean droplet (2GB RAM / 1-2 CPU tier).
# Run as the non-root deploy user (needs passwordless sudo), NOT as root — it
# relies on that user's own SSH key for the GitHub clone.
#
#   REPO_URL=git@github.com:you/your-repo.git bash install.sh
#
# Safe to re-run — every step checks whether it already happened.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if [ "$(id -u)" -eq 0 ]; then
  echo "!!  Run this as the deploy user (not root) — it uses that user's own" >&2
  echo "!!  GitHub SSH key to clone the repo. Try: su - deploy" >&2
  exit 1
fi

REPO_URL="${REPO_URL:?Set REPO_URL to your git remote, e.g. REPO_URL=git@github.com:you/your-repo.git}"
APP_DIR="${APP_DIR:-$HOME/garmin-connector}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

echo "==> Updating system packages"
sudo apt-get update -y
sudo apt-get -y -o Dpkg::Options::="--force-confold" upgrade

echo "==> Installing base packages"
sudo apt-get install -y --no-install-recommends ca-certificates curl git ufw

echo "==> Setting up ${SWAP_SIZE} swap (small droplet — docker builds can spike RAM)"
if [ ! -f /swapfile ]; then
  sudo fallocate -l "$SWAP_SIZE" /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
  echo "    swapfile already exists, skipping"
fi

echo "==> Installing Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "    docker already installed, skipping"
fi

if ! groups "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  echo "    added $USER to the docker group (takes effect in new sessions; this run still uses sudo for docker)"
fi

echo "==> Configuring firewall (SSH, HTTP, HTTPS only)"
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

echo "==> Fetching application code"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  echo "    already cloned at $APP_DIR, pulling latest"
  git -C "$APP_DIR" pull
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  cat <<EOF

!!  No .env found at $APP_DIR/.env — the app needs real secrets before it can start.
!!  From your machine, copy your local .env up (never commit it to git):
!!
!!      scp .env deploy@$(curl -s -4 ifconfig.me 2>/dev/null || echo "<droplet-ip>"):$APP_DIR/.env
!!
!!  Make sure .env also sets:
!!      DOMAIN=yourdomain.com
!!      WEBAPP_URL=https://yourdomain.com
!!      DEV_MODE=false
!!
!!  Then re-run: bash deploy/install.sh

EOF
  exit 1
fi

if grep -qE '^DEV_MODE=true' .env; then
  echo "!!  WARNING: DEV_MODE=true in .env — this bypasses Telegram login entirely. Set it to false for production." >&2
fi

if ! grep -qE '^DOMAIN=' .env; then
  echo "!!  ERROR: .env is missing DOMAIN=yourdomain.com, needed for Caddy's automatic HTTPS." >&2
  exit 1
fi

echo "==> Building and starting the app (this can take a few minutes on a small droplet)"
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

echo "==> Waiting for the backend to be healthy"
for _ in $(seq 1 60); do
  if sudo docker compose exec -T backend python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "==> Running database migrations"
sudo docker compose exec -T backend alembic upgrade head

echo
echo "==> Done."
echo "    Point your domain's DNS A record at this droplet's IP if you haven't already."
echo "    Caddy will provision HTTPS automatically once DNS resolves."
echo "    Check status: sudo docker compose ps"
echo "    Check logs:   sudo docker compose logs -f backend"
echo "    (log out and back in as $USER to use docker without sudo from now on)"
