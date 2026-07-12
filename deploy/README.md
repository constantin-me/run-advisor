# Deploying to a DigitalOcean droplet

Tested against the smallest droplet tier: 1-2 vCPU, 2GB RAM, 25GB disk, Ubuntu 24.04.

You create the droplet yourself (Ubuntu 24.04, any size ≥ 2GB RAM, your SSH
key added at creation). Everything after that is one script.

## 1. Push this repo to a git remote

`install.sh` clones from git, so the code needs to be on GitHub/GitLab first
(private repo recommended — `.env` itself is gitignored and never committed,
but keeping the repo private is still good practice).

## 2. Point a domain at the droplet

Telegram Mini Apps require a real public HTTPS URL — self-signed certs don't
work inside Telegram's WebView. Add an **A record** for your domain (or a
subdomain, e.g. `coach.example.com`) pointing at the droplet's IP. DNS can
take a few minutes to propagate; Caddy waits for it and provisions a Let's
Encrypt certificate automatically once it resolves.

## 3. SSH in and run the installer

```
ssh root@<droplet-ip>
REPO_URL=https://github.com/you/garmin-connector.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/you/garmin-connector/main/deploy/install.sh)"
```

Or copy the script up first and run it directly:

```
scp deploy/install.sh root@<droplet-ip>:/root/
ssh root@<droplet-ip>
REPO_URL=https://github.com/you/garmin-connector.git bash install.sh
```

This installs Docker, adds a 2GB swapfile (small droplets can OOM during the
webapp/Python build otherwise), opens the firewall for SSH/HTTP/HTTPS only,
and clones the repo to `/opt/garmin-connector`. It will then stop and tell
you it needs a `.env` file — that's expected on the first run.

## 4. Copy up your `.env`

From your machine, using your real local `.env` as the base:

```
scp .env root@<droplet-ip>:/opt/garmin-connector/.env
```

Then edit it on the droplet (`ssh root@<droplet-ip>` then
`nano /opt/garmin-connector/.env`) to set:

```
DOMAIN=coach.example.com
WEBAPP_URL=https://coach.example.com
DEV_MODE=false
```

`DEV_MODE=false` is important — leaving it `true` bypasses Telegram login
entirely (`/api/auth/dev-session`), meant only for local development.

## 5. Re-run the installer

```
bash install.sh
```

Same command as step 3 — it's idempotent, so running it again now picks up
the `.env` it was missing before, builds the app, and starts everything.

## 6. Point your bot at the real URL

In @BotFather: set the Mini App URL to `https://coach.example.com`.

## Redeploying after code changes

```
cd /opt/garmin-connector
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose exec backend alembic upgrade head   # only if there's a new migration
```

## Notes

- **Postgres data** lives in a named Docker volume (`pg_data`) — it survives
  container reboots/rebuilds. Back it up with
  `docker compose exec postgres pg_dump -U coach coach > backup.sql` before
  anything destructive.
- **Garmin tokens** live in the `garmin_tokens` volume — also persistent.
- **Caddy** owns ports 80/443 and handles HTTPS certs automatically (stored
  in the `caddy_data` volume); nothing else needs a manual cert.
- The backend container only binds to `127.0.0.1:8010` on the host — it's
  not reachable from outside except through Caddy. For debugging directly,
  SSH in and `curl localhost:8010/api/health`, or tunnel:
  `ssh -L 8010:localhost:8010 root@<droplet-ip>`.
- This droplet tier runs everything (Postgres, backend, Caddy) on one box.
  Fine for personal/single-user use; if this ever needs to scale, Postgres
  should move to a managed database first.
