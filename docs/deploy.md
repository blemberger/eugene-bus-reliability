# Deploying: private GitHub repo + a small server

The pipeline runs unattended on a rented Linux server with the same `docker compose`
file used locally. GitHub (private) is how the code gets there and is backed up.
The public dashboard is a later step and needs nothing here.

## 1. GitHub — private repository (30 minutes)

On your laptop, in the project folder, in Ubuntu:

```bash
git init -b main
git add .
git status          # .env, data/, .venv/, dump.txt must NOT appear — .gitignore handles them
git commit -m "LTD transit reliability: collector, analysis layer, dashboard"
```

On github.com: New repository → name `ltd-transit-reliability` → **Private** → no README,
no .gitignore, no license (you have them) → Create. Then, using the SSH URL it shows:

```bash
ssh-keygen -t ed25519 -C "laptop"          # once; press Enter for defaults
cat ~/.ssh/id_ed25519.pub                  # add this at github.com → Settings → SSH and GPG keys
git remote add origin git@github.com:blemberger/ltd-transit-reliability.git
git push -u origin main
```

From now on, after each change: `git add -A && git commit -m "what changed" && git push`.
This history stays private. When it's time to go public, `make strip-learn`, copy the tree
to a fresh folder, `git init` there, and push that as a new repository; the private history stays private.

## 2. The server (one evening)

**Provider.** Hetzner Cloud, CX22 (2 vCPU, 4 GB RAM, 40 GB disk, ~€4/month), Ubuntu 24.04,
location Hillsboro (Oregon). 4 GB matters: the dbt build needs headroom; 1 GB servers crash.
Add your laptop's SSH public key (`~/.ssh/id_ed25519.pub`) when creating the server so
there is no password login at all.

**First login and hardening (10 minutes).** Replace `1.2.3.4` with the server's IP.

```bash
ssh root@1.2.3.4
adduser ben && usermod -aG sudo ben
rsync --archive --chown=ben:ben ~/.ssh /home/ben     # your key works for ben too
ufw allow OpenSSH && ufw --force enable               # firewall: only SSH is open
apt update && apt install -y unattended-upgrades git rsync make
exit
ssh ben@1.2.3.4
```

**Docker.**

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ben
exit; ssh ben@1.2.3.4          # log back in so the docker group applies
docker run --rm hello-world     # prints a greeting; then it works
```

**Code.** Give the server read-only access to the private repo with a *deploy key*:

```bash
ssh-keygen -t ed25519 -C "ltd-server" -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub       # github.com → the repo → Settings → Deploy keys → Add (read-only)
git clone git@github.com:blemberger/ltd-transit-reliability.git
cd ltd-transit-reliability
cp .env.example .env && nano .env          # new, strong POSTGRES_PASSWORD; same in all three URLs
```

**Run.**

```bash
make up && sleep 20 && make migrate && make load-static && make run
docker compose ps               # four containers Up
make logs                       # watch a few cycles; Ctrl+C
```

`make migrate` creates the read-only dashboard user with the password from `.env`; the
scheduler's dbt build grants it access and fails if it doesn't exist, so migrate first.

The server is now collecting. Nothing on it is reachable from the internet except SSH:
`docker-compose.yml` binds Postgres to 127.0.0.1 only, and the firewall allows only port 22.

## 3. Moving the history you already collected

The raw archive is the source of truth; copy it up and rebuild the tables from it.
On your **laptop**, with `VPS_HOST=ben@1.2.3.4` in `.env`:

```bash
make push-archive               # rsync of data/raw — a few hundred MB
```

On the **server**:

```bash
make rebuild                    # replays the archive; the poller keeps running meanwhile
make dbt-build                  # first analysis build; then the scheduler does it hourly
```

Then stop collecting on the laptop so there is one source of truth: `make down` there.
Keep the laptop's `data/raw` as a second copy of the archive.

## 4. Looking at the server's data from your laptop

Postgres on the server is not exposed, and shouldn't be. An SSH tunnel makes it look local:

```bash
make tunnel        # terminal 1: stays open; forwards server:5432 to localhost:5432
make app           # terminal 2: the dashboard, reading the server's database
```

(Your laptop's own Postgres must be down first — `make down` — or the ports collide.)

## 5. Updating the server after code changes

On the laptop: commit and push. On the server:

```bash
cd ~/ltd-transit-reliability && git pull && make run
```

`make run` rebuilds the image and restarts poller and scheduler; the database is untouched.
If a change added a migration: `make migrate` before `make run`. If it changed dbt models:
they take effect at the next hourly build, or `make dbt-build` now.

## 6. Backups

The archive is the backup of everything realtime; the schedule reloads from LTD. Once a
week, from the laptop: `rsync -avz ben@1.2.3.4:~/ltd-transit-reliability/data/raw/ data/raw/`
pulls new archive files down. Object storage (S3) replaces this when the archive outgrows
the laptop, which at ~300 MB/month is a long way off.

## 7. The public dashboard, on the same server (when ready)

The dashboard runs identically on the server: same code, same pages, live map included.
It only needs a hostname for HTTPS. Point a subdomain (e.g. `transit.benlemberger.com`)
at the server's IP with an A record, then on the server:

```bash
nano .env                       # SITE_ADDRESS=transit.benlemberger.com
sudo ufw allow 80 && sudo ufw allow 443
docker compose --profile web up -d --build
```

Caddy obtains and renews the certificate itself. To test before DNS exists, leave
`SITE_ADDRESS=:80`, open port 80, and visit `http://<server-ip>`. To take it down:
`docker compose --profile web down` (the collector keeps running).

The dashboard connects as the read-only database user, so nothing typed into its SQL
box can change data. Until the repository is public you may want the site password-
protected: add `basicauth` to the Caddyfile (`caddy hash-password` makes the hash).