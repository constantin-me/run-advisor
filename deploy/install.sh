#!/usr/bin/env bash
# One-shot install for a fresh Ubuntu DigitalOcean droplet (2GB RAM / 1-2 CPU tier).
# SSH into the droplet you created, then run:
#   REPO_URL=https://github.com/you/garmin-connector.git bash install.sh
#
# Safe to re-run — every step checks whether it already happened.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO_URL="${REPO_URL:?Set REPO_URL to your git remote, e.g. REPO_URL=https://github.com/you/garmin-connector.git}"
APP_DIR="${APP_DIR:-/opt/garmin-connector}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

echo "==> Updating system packages"
apt-get update -y
apt-get -y -o Dpkg::Options::="--force-confold" upgrade

echo "==> Installing base packages"
apt-get install -y --no-install-recommends ca-certificates curl git ufw

echo "==> Setting up ${SWAP_SIZE} swap (small droplet — docker builds can spike RAM)"
if [ ! -f /swapfile ]; then
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  echo "    swapfile already exists, skipping"
fi

echo "==> Installing Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "    docker already installed, skipping"
fi

echo "==> Configuring firewall (SSH, HTTP, HTTPS only)"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

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
!!      scp .env root@$(curl -s -4 ifconfig.me 2>/dev/null || echo "<droplet-ip>"):$APP_DIR/.env
!!
!!  Make sure .env also sets:
!!      DOMAIN=yourdomain.com
!!      WEBAPP_URL=https://yourdomain.com
!!      DEV_MODE=false
!!
!!  Then re-run this script, or just run:
!!      cd $APP_DIR && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

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
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

echo "==> Waiting for the backend to be healthy"
for _ in $(seq 1 60); do
  if docker compose exec -T backend python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "==> Running database migrations"
docker compose exec -T backend alembic upgrade head

echo
echo "==> Done."
echo "    Point your domain's DNS A record at this droplet's IP if you haven't already."
echo "    Caddy will provision HTTPS automatically once DNS resolves."
echo "    Check status: docker compose ps"
echo "    Check logs:   docker compose logs -f backend"
