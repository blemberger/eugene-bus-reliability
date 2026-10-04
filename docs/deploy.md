# Running it on a server

How eugenebuswatch.com runs: one small Linux server running the same `docker compose`
file as a laptop, with Caddy in front for HTTPS. The code reaches the server from GitHub,
and every later update is one command from the laptop: `make deploy`.

Two terminals appear below. **Laptop** means the project folder on your own computer, with
the venv active. **Server** means a shell on the server, opened with `ssh root@<server ip>`.

## 1. The server

**Provider.** A DigitalOcean droplet, Ubuntu 24.04, 2 GB RAM (the dbt build needs the
headroom; 1 GB is too small), any region. Add your laptop's SSH public key
(`~/.ssh/id_ed25519.pub`; `ssh-keygen -t ed25519` makes one) when creating it, so the
server accepts keys only and never passwords.

**Software and firewall** (server):

```bash
apt update && apt install -y git make unattended-upgrades
curl -fsSL https://get.docker.com | sh
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

**Code and settings** (server):

```bash
git clone https://github.com/blemberger/eugene-bus-reliability.git /opt/eugene-bus-reliability
cd /opt/eugene-bus-reliability
cp .env.example .env
nano .env    # new passwords (openssl rand -hex 24), the same POSTGRES_PASSWORD inside both URLs
```

## 2. Start collecting (server)

```bash
make up && make migrate && make run
docker compose ps        # db, db_test, poller, scheduler: all Up
make logs                # watch a few poll cycles, then Ctrl+C
```

`make migrate` sets the read-only site user's password from `READER_PASSWORD`. The
scheduler loads LTD's schedule at startup and then builds the analysis every 15 minutes.

## 3. The site on its own domain

1. **DNS.** At the domain's DNS host, add two A records pointing at the server's IP: one
   for the bare name (`eugenebuswatch.com`) and one for `www`.
2. **Site address** (server): in `.env`, the names separated by a space, no quotes:

   ```
   SITE_ADDRESS=eugenebuswatch.com www.eugenebuswatch.com
   ```

3. **Start the site** (server): `docker compose --profile web up -d --build`

Caddy gets and renews the HTTPS certificates itself once DNS points at the server, and
sends the `www` name to the bare one. Before DNS exists, `SITE_ADDRESS=:80` serves plain
HTTP on the server's IP. The site connects as the read-only database user, so nothing on
it can change data.

## 4. Updating the server after a code change

Once, on the laptop, add the server to `.env`: `VPS_HOST=root@<server ip>`. Then, after
committing and pushing a change (laptop):

```bash
make deploy
```

It checks that the laptop matches GitHub, then on the server: pulls the code, applies any
new migrations, rebuilds and restarts what changed, and reloads Caddy. The database and
the raw archive are untouched. Model changes take effect at the next 15-minute build, or
immediately with `make server-dbt-build` run on the server. When the analysis code changed,
that build recomputes every day (a full refresh, 10-20 minutes); other builds recompute only
the last two days.

## 5. Checking on it

- **Laptop:** `make fetch-dump` builds a full diagnostic snapshot on the server (collection
  health, outages, the analysis, every page rendered, memory, disk, log errors) and
  copies it to `dump.txt`.
- **Laptop:** `make visitors` reports the site's visitors: visits and page views per day,
  share on phones, the pages and stops people looked at, and the other sites that sent
  them (from Caddy's access log). The site records only the time, the page, phone or
  computer, and a random id per browser tab; no IP addresses or cookies.
- **Site:** the Status page shows what came in during the last few minutes and when the
  analysis last ran.
- **Server:** `make server-db-busy` lists anything the database has been running for more
  than 5 seconds. Closing a terminal (or Ctrl+C on `ssh`) does not stop a query already
  running on the server; this shows it, and its `pid` stops it with
  `select pg_terminate_backend(<pid>)`.

## 6. Backups

- **Raw archive.** It can rebuild every realtime table (`make rebuild`). From the laptop,
  now and then: `rsync -avz root@<server ip>:/opt/eugene-bus-reliability/data/raw/ data/raw/`
- **Whole server.** DigitalOcean's weekly backups (droplet → Backups) cover the database too.

## 7. Looking at the server's database from the laptop

The database isn't reachable from the internet. An SSH tunnel makes it appear on the
laptop at port 55439 (laptop, leave it running):

```bash
make tunnel
```

Then, in a second laptop terminal, run the site against it, using the server's
`READER_PASSWORD`:

```bash
make app DATABASE_URL=postgresql://ltd_reader:<server READER_PASSWORD>@localhost:55439/ltd
```