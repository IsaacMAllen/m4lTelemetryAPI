# Deploying m4l-telemetry-api to a RackNerd Ubuntu VPS

> **You are here:** taking the API from "running on my laptop in kind" to
> "running in production on a Linux box at https://api.yourdomain.com".

This guide is opinionated. Every choice has a justification next to it so
you can decide later that you want something different. The goal is **the
correct way, not the fanciest way.**

---

## Prerequisites checklist

Before you start, get all of these:

- [ ] A RackNerd VPS running **Ubuntu 22.04 or 24.04**, ≥ 1 GB RAM, ≥ 25 GB disk.
- [ ] **Root SSH access** to it (RackNerd emails you the IP + root password
      after provisioning). You'll harden this away in step 1.
- [ ] An **SSH key** on your laptop (`~/.ssh/id_ed25519.pub` or similar).
      If not: `ssh-keygen -t ed25519`.
- [ ] A **domain you own** (e.g. `bugbytz.com`). Anywhere is fine, but you
      need access to its DNS (Cloudflare, Namecheap, Route 53, whatever).
- [ ] A **GitHub repo** for `m4lTelemetryAPI` (you already have this).
- [ ] A **Backblaze B2 account** for backups (free, 10 GB included).
      <https://www.backblaze.com/b2/sign-up.html>
- [ ] **Ansible** on your laptop:  `brew install ansible` (or `pipx install ansible`).
- [ ] Required Ansible collections:  `ansible-galaxy collection install community.general community.docker`.

You'll also pick three names. Decide them now and stay consistent:

| Name             | Example                       |
| ---------------- | ----------------------------- |
| API hostname     | `api.bugbytz.com`             |
| Frontend host    | `bytr.bugbytz.com`            |
| GHCR image path  | `ghcr.io/bugbytz/m4l-telemetry-api` |

---

## The architecture, one more time

```
                                 (TLS terminates here)
   bz.telemetry ──HTTPS──►  Caddy ──HTTP loopback──►  uvicorn (api container)
                              ↑                              │
                              └─ Let's Encrypt cert          │
                                                             ▼
                                                       postgres container
                                                       (named volume)
                                                             │
                                                  pg_dump | restic
                                                             │
                                                             ▼
                                                       Backblaze B2 bucket
```

Everything except `pg_dump | restic` is reachable only through Caddy on
ports 80/443. Postgres is **never** exposed on the public internet.

---

## Phase 1 — One-time server bootstrap (5 min, manual)

We need a non-root user before Ansible can SSH in. Five commands.

```bash
# from your laptop, in this repo:
cd ~/development/m4lTelemetryAPI/deploy/scripts

# copy your laptop's SSH pubkey to the VPS root account (RackNerd asks for
# the root password it emailed you).  Skip if you've already done this.
ssh-copy-id root@<VPS_IP>

# upload + run the bootstrap script
scp bootstrap-server.sh root@<VPS_IP>:/root/
ssh root@<VPS_IP> 'bash /root/bootstrap-server.sh'
```

That script:

1. Creates the `deploy` user with passwordless sudo.
2. Copies your SSH pubkey to `deploy@<VPS_IP>`.
3. Disables root login + password auth in sshd.
4. Installs UFW and allows only `ssh`, `http`, `https`.

**Verify** (this is the box's new front door):

```bash
ssh deploy@<VPS_IP> 'whoami; sudo -n true && echo sudo-ok'
# expected:
#   deploy
#   sudo-ok
```

If it works, you're done with Phase 1. The `root@` account is now firewalled
off — you'll never SSH as root again.

---

## Phase 2 — Configure the box with Ansible (15 min)

```bash
cd ~/development/m4lTelemetryAPI/deploy/ansible

# Set up your real inventory + vars (gitignored)
cp inventory.example.yml inventory.yml
mkdir -p group_vars
cp group_vars.example.yml group_vars/all.yml

# Edit BOTH files: VPS IP, your domain, your email, your GHCR path
$EDITOR inventory.yml
$EDITOR group_vars/all.yml

# Smoke test: can Ansible reach the box?
ansible -i inventory.yml telemetry -m ping
# expected: m4l-telemetry-prod | SUCCESS => { ... "ping": "pong" }

# Run the full playbook (this takes ~10 min on a fresh box)
ansible-playbook -i inventory.yml playbook.yml
```

What this does, in order, on the VPS:

1. **`base`** — installs `ufw`, `fail2ban`, `unattended-upgrades`. Sets the
   timezone to UTC. Configures auto-applied security patches.
2. **`docker`** — adds the official Docker apt repo, installs `docker-ce`
   and the Compose v2 plugin, adds `deploy` to the `docker` group.
3. **`caddy`** — adds the Cloudsmith Caddy apt repo, installs Caddy, writes
   `/etc/caddy/Caddyfile` from `templates/Caddyfile.j2`.
4. **`app`** — creates `/opt/m4l-telemetry/` owned by `deploy`, lays down
   `docker-compose.yml`, generates `/opt/m4l-telemetry/.env` **with random
   48-char secrets** (only on first run; never overwritten).
5. **`backups`** — installs `restic`, drops the backup script in
   `/usr/local/bin/m4l-telemetry-backup`, schedules a 03:17 UTC cron.

When it finishes, **Caddy is running** but won't have a cert yet (DNS isn't
pointing here). That's fine — next phase.

> **Idempotent:** re-run this playbook anytime. It's safe. The `.env` file
> in particular is never re-rendered after the first time, so secrets stay
> stable across runs.

---

## Phase 3 — Point DNS at the VPS (5 min)

At your domain registrar / DNS provider, create:

| Record | Name              | Value                      | TTL  |
| ------ | ----------------- | -------------------------- | ---- |
| A      | `api.bugbytz.com` | `<VPS_IP>`                 | 300  |

(Replace with your real domain.)

Wait for it to resolve:

```bash
dig +short api.bugbytz.com    # should return your VPS IP
```

As soon as DNS resolves, hit the VPS once over HTTPS:

```bash
curl -I https://api.bugbytz.com/
# the FIRST request triggers Caddy's Let's Encrypt cert issuance.  This
# can take 10-30 seconds; after that you'll see HTTP/2 200 instantly.
```

If you see a valid TLS cert in your browser, **TLS is done forever**. Caddy
auto-renews 30 days before expiry.

---

## Phase 4 — Fill in the real production secrets (5 min)

Ansible generated random secrets on the box. Three of them you'll want to
*read* (you need to give the ingest token to bz.telemetry); two of them you
need to *fill in* (the Backblaze keys).

```bash
ssh deploy@<VPS_IP>
sudo cat /opt/m4l-telemetry/.env
```

You'll see something like:

```
POSTGRES_PASSWORD=…48 random chars…
TELEMETRY_INGEST_TOKENS=…48 random chars…   ← copy this for bz.telemetry
RESTIC_REPOSITORY=b2:m4l-telemetry-backups:m4l-telemetry
RESTIC_PASSWORD=…48 random chars…
B2_ACCOUNT_ID=FILL_ME_IN                     ← from Backblaze
B2_ACCOUNT_KEY=FILL_ME_IN                    ← from Backblaze
```

1. Go to <https://secure.backblaze.com/app_keys.htm>, **Add a New
   Application Key** scoped to your `m4l-telemetry-backups` bucket. Save
   `keyID` (= `B2_ACCOUNT_ID`) and `applicationKey` (= `B2_ACCOUNT_KEY`).
2. Edit the file in place — `sudo nano /opt/m4l-telemetry/.env` — and paste
   them in.
3. Restart the stack so it re-reads `.env`:
   ```bash
   cd /opt/m4l-telemetry && docker compose up -d
   ```
4. **Run a one-shot backup right now** to verify B2 works:
   ```bash
   sudo /usr/local/bin/m4l-telemetry-backup
   sudo restic snapshots          # should list one snapshot
   ```

> **Why `.env`, not k8s `Secret` / sops / Vault?** A single VPS doesn't
> have a control plane to securely fetch secrets from. The threat model is
> "anyone with shell on this box can read the file" — which is true of
> *every* secrets approach on a single host. We pin permissions to
> `chmod 600` owned by `deploy`, never check it in. If you later add a
> second host, that's when sops or HashiCorp Vault start paying off.

---

## Phase 5 — Wire up GitHub Actions auto-deploy (10 min)

The included workflow is at `.github/workflows/deploy.yml`. It needs three
secrets and one variable in your GitHub repo settings.

1. **Generate a deploy SSH key locally** (separate from your personal key):
   ```bash
   ssh-keygen -t ed25519 -f /tmp/deploy_key -C "gha-deploy" -N ""
   ```
2. **Append the pubkey to the VPS's authorized_keys**:
   ```bash
   cat /tmp/deploy_key.pub | ssh deploy@<VPS_IP> 'cat >> ~/.ssh/authorized_keys'
   ```
3. **In your GitHub repo → Settings → Secrets and variables → Actions:**

   | Type     | Name              | Value                                |
   | -------- | ----------------- | ------------------------------------ |
   | Secret   | `VPS_HOST`        | `<VPS_IP>`                           |
   | Secret   | `VPS_USER`        | `deploy`                             |
   | Secret   | `VPS_SSH_KEY`     | **contents of `/tmp/deploy_key`**    |
   | Secret   | `VPS_HOST_FQDN`   | `api.bugbytz.com` (smoke check uses this) |
   | Variable | `API_INSTALL_DIR` | `/opt/m4l-telemetry`                 |

   Then `rm /tmp/deploy_key /tmp/deploy_key.pub` on your laptop.

4. **Make the GHCR image package public** so the VPS can pull without auth:
   GitHub repo → **Packages** → `m4l-telemetry-api` → **Package settings**
   → **Change package visibility → Public**. (The image is just compiled
   Python — it has no secrets.)

5. **First deploy:** push a commit (or run the workflow manually).
   ```bash
   git commit --allow-empty -m "ci: trigger first prod deploy"
   git push
   ```
   Watch it in **Actions** → `deploy`. It will:
   - Build a `linux/amd64` image of the API.
   - Push to `ghcr.io/<owner>/m4l-telemetry-api:<short-sha>` and `:latest`.
   - SSH to the VPS, write a `docker-compose.override.yml` pinning the new
     SHA, run migrations, then `up -d --no-deps api`.
   - Smoke `https://api.bugbytz.com/healthz`.

   Every push to `main` from now on is a one-line deploy.

---

## Phase 6 — Point bz.telemetry at production (2 min)

Inside Max, on your `bz.telemetry` device:

```
@endpoint    https://api.bugbytz.com/v1/events
@auth_token  <the value of TELEMETRY_INGEST_TOKENS from /opt/m4l-telemetry/.env>
```

Send a test event. Then on the VPS:

```bash
ssh deploy@<VPS_IP> 'docker compose -f /opt/m4l-telemetry/docker-compose.yml \
    exec -T postgres psql -U telemetry -d telemetry \
    -c "select count(*) from events;"'
```

If the count went up, you're live in production.

---

## Phase 7 — Deploy bytr to Cloudflare Pages (5 min)

1. **Cloudflare → Pages → Create a project → Connect to Git** → pick the
   `bytr` repo.
2. **Build settings:**
   - **Framework preset:** None (Vite)
   - **Build command:** `pnpm install --frozen-lockfile && pnpm build`
   - **Build output directory:** `dist`
   - **Root directory:** *(leave blank)*
3. **Environment variables** (Production):
   - `VITE_API_URL` = `https://api.bugbytz.com`
   - `NODE_VERSION` = `22`
4. **Custom domain** (after first deploy): `bytr.bugbytz.com` → CNAME to
   `<project>.pages.dev`. Cloudflare handles TLS.
5. Make sure the API allows the Pages origin in CORS. You already set
   `api_cors_origins` in `group_vars/all.yml`. If you change it later:
   ```bash
   cd deploy/ansible
   ansible-playbook -i inventory.yml playbook.yml --tags app
   ```

   That re-renders `docker-compose.yml` with the new origins and restarts
   the API container.

> **Heads up:** Cloudflare Pages serves from the edge, so the bytr bundle
> is on a different origin than the API. The browser will do a CORS
> preflight OPTIONS for every API request. The API already handles this
> when `TELEMETRY_CORS_ORIGINS` is set.

---

## Day-2 operations cheatsheet

```bash
# read recent app logs
ssh deploy@<VPS_IP> 'docker compose -f /opt/m4l-telemetry/docker-compose.yml logs --tail=200 api'

# inspect Caddy's TLS state
ssh deploy@<VPS_IP> 'sudo journalctl -u caddy --since "10 min ago"'

# manually trigger a backup
ssh deploy@<VPS_IP> 'sudo /usr/local/bin/m4l-telemetry-backup'

# list backup snapshots
ssh deploy@<VPS_IP> 'sudo bash -c "source /opt/m4l-telemetry/.env && restic snapshots"'

# rotate the ingest token
#   1. ssh in, edit /opt/m4l-telemetry/.env
#   2. cd /opt/m4l-telemetry && docker compose up -d
#   3. update bz.telemetry @auth_token in Max

# rollback to a previous image
#   On GitHub → Actions → pick the workflow run that matches the SHA you
#   want, click "Re-run all jobs".  That re-pushes :latest and re-deploys.
#   Or manually:
ssh deploy@<VPS_IP>
cd /opt/m4l-telemetry
sed -i 's|m4l-telemetry-api:.*|m4l-telemetry-api:<good-sha>|' docker-compose.override.yml
docker compose pull api && docker compose up -d --no-deps api
```

---

## Restoring from a backup (worst-case)

```bash
ssh deploy@<VPS_IP>
sudo bash -c '
    source /opt/m4l-telemetry/.env
    cd /opt/m4l-telemetry
    SNAPSHOT_ID=$(restic snapshots --json | jq -r ".[-1].id")
    docker compose down
    docker volume rm m4l-telemetry-pgdata
    docker compose up -d postgres
    sleep 5
    restic dump "$SNAPSHOT_ID" /m4l-telemetry-pg.dump | \
        docker compose exec -T postgres pg_restore -U telemetry -d telemetry --clean --if-exists
    docker compose up -d
'
```

Practice this once on a non-production box before you need it.

---

## Cost ballpark (monthly, USD)

| Item                   | Cost                                  |
| ---------------------- | ------------------------------------- |
| RackNerd VPS           | $10–30 (depending on plan)            |
| Domain                 | ~$1                                   |
| Backblaze B2 (10 GB)   | $0 (free tier)                        |
| Cloudflare Pages       | $0 (free tier, generous)              |
| **Total**              | **~$11–31/month**                     |

---

## Things this guide intentionally does **not** do

- **HA / multi-region.** Single box. Acceptable for a personal/beta product;
  bz.telemetry buffers locally and retries, so a few hours of API downtime
  doesn't lose events.
- **Postgres replication.** If the VPS dies catastrophically, you lose
  whatever's been written since last night's 03:17 UTC backup. Move to
  Neon/Supabase/RDS the day this isn't acceptable.
- **WAF / DDoS protection.** Throw Cloudflare in front of `api.bugbytz.com`
  if you start getting hammered. (Set Cloudflare to "Full (strict)" SSL
  mode and you're good.)
- **Centralised logging.** `docker logs` is enough at this scale. When
  it isn't, ship to Better Stack / Logtail / Grafana Cloud (free tiers
  are generous).
