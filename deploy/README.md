# Deploying to a DigitalOcean droplet

Tested against the smallest droplet tier: 1-2 vCPU, 2GB RAM, 25GB disk, Ubuntu 24.04.

You create the droplet yourself. Everything after that is: create a
non-root deploy user with its own GitHub key, then run one script.

## 1. Create a non-root deploy user with a GitHub deploy key

Don't run the app as root. SSH in as root once to set this up:

```
ssh root@<droplet-ip>
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
echo "deploy ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/deploy
chmod 440 /etc/sudoers.d/deploy
mkdir -p /home/deploy/.ssh && cp /root/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys
```

Then, as `deploy`, generate a dedicated key for GitHub (kept separate from
your personal key, and scoped read-only via a repo Deploy Key rather than
full account access):

```
su - deploy
ssh-keygen -t ed25519 -C "deploy@<hostname>" -f ~/.ssh/github_deploy_key -N ""
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy_key
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
cat ~/.ssh/github_deploy_key.pub
```

Add the printed public key on GitHub: repo → **Settings → Deploy keys → Add
deploy key**. Leave "Allow write access" **unchecked** — the droplet only
needs to pull.

Verify it works before moving on:

```
ssh -T git@github.com   # should greet you by username, not error
git ls-remote git@github.com:you/your-repo.git
```

## 2. Point a domain at the droplet

Telegram Mini Apps require a real public HTTPS URL — self-signed certs don't
work inside Telegram's WebView.

**If your domain is proxied through Cloudflare** (orange cloud), automatic
Let's Encrypt via Caddy doesn't work cleanly — public traffic hits
Cloudflare's edge, not your droplet directly. Instead:

1. Add an **A record** for your domain/subdomain pointing at the droplet's
   IP (proxy status: proxied/orange cloud is fine — that's the point).
2. In Cloudflare: **SSL/TLS → Origin Server → Create Certificate**. Remove
   the default suggested hostnames and enter only the exact hostname you're
   using (e.g. `coach.example.com`) — don't use a wildcard unless you
   actually want the cert to cover every subdomain; a single hostname
   already excludes the apex domain and everything else by default.
   15-year validity, RSA key. Copy the **Origin Certificate** and **Private
   Key** shown (only shown once).
3. On the droplet, put them at `~/garmin-connector/deploy/certs/origin.pem`
   and `~/garmin-connector/deploy/certs/origin-key.pem` (gitignored, never
   commit these — see step 4).
4. In Cloudflare: **SSL/TLS → Overview → set mode to "Full (strict)"** so
   Cloudflare validates this cert on the connection to your origin too —
   real end-to-end encryption, not just Cloudflare-to-client.

**If your domain isn't proxied** (DNS only / grey cloud, or not on
Cloudflare at all), skip the above — Caddy's automatic Let's Encrypt just
works once the A record resolves directly to the droplet. Revert
`deploy/Caddyfile` to `{$DOMAIN} { reverse_proxy backend:8000 }` (drop the
`tls` line) if you're in this case.

## 3. Run the installer (as `deploy`, not root)

```
ssh deploy@<droplet-ip>
REPO_URL=git@github.com:you/your-repo.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/you/your-repo/main/deploy/install.sh)"
```

Or copy the script up first:

```
scp deploy/install.sh deploy@<droplet-ip>:~/
ssh deploy@<droplet-ip>
REPO_URL=git@github.com:you/your-repo.git bash install.sh
```

It refuses to run as root — it needs to run as `deploy` so git uses that
user's own GitHub key. This installs Docker, adds a 2GB swapfile (small
droplets can OOM during the webapp/Python build otherwise), opens the
firewall for SSH/HTTP/HTTPS only, and clones the repo to
`~/garmin-connector`. It then stops and tells you it needs a `.env` file —
expected on the first run.

## 4. Copy up your `.env` and origin certificate

From your machine, using your real local `.env` as the base:

```
scp .env deploy@<droplet-ip>:~/garmin-connector/.env
```

If you're using a Cloudflare Origin Certificate (step 2), also copy the two
files you saved from the Cloudflare dashboard:

```
scp origin.pem origin-key.pem deploy@<droplet-ip>:~/garmin-connector/deploy/certs/
```

Then edit `.env` on the droplet (`ssh deploy@<droplet-ip>` then
`nano ~/garmin-connector/.env`) to set:

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
cd ~/garmin-connector
git pull
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
sudo docker compose exec backend alembic upgrade head   # only if there's a new migration
```

(Log out and back in once after the first install and you can drop `sudo`
from the `docker` commands — `install.sh` adds `deploy` to the `docker`
group, which only takes effect in a fresh session.)

## Notes

- **Postgres data** lives in a named Docker volume (`pg_data`) — it survives
  container reboots/rebuilds. Back it up with
  `docker compose exec postgres pg_dump -U coach coach > backup.sql` before
  anything destructive.
- **Garmin tokens** live in the `garmin_tokens` volume — also persistent.
- **Caddy** owns ports 80/443. With a Cloudflare Origin Certificate it just
  serves the static cert from `deploy/certs/` — no renewal needed for 15
  years. Without Cloudflare in front, it provisions Let's Encrypt certs
  automatically instead (stored in the `caddy_data` volume).
- The backend container only binds to `127.0.0.1:8010` on the host — it's
  not reachable from outside except through Caddy. For debugging directly,
  SSH in and `curl localhost:8010/api/health`, or tunnel:
  `ssh -L 8010:localhost:8010 deploy@<droplet-ip>`.
- This droplet tier runs everything (Postgres, backend, Caddy) on one box.
  Fine for personal/single-user use; if this ever needs to scale, Postgres
  should move to a managed database first.
