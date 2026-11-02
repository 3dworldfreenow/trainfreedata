# Installation — Ubuntu 24.04 LTS

Target layout: code in `/opt/railmon/app`, virtualenv in `/opt/railmon/venv`,
services run as the unprivileged `railmon` user behind Apache.

## 1. System packages

```bash
sudo apt update
```

```bash
sudo apt install -y python3 python3-venv python3-dev build-essential git mysql-server redis-server apache2 certbot python3-certbot-apache
```

## 2. User and code

```bash
sudo useradd --system --home /opt/railmon --shell /usr/sbin/nologin railmon
```

```bash
sudo mkdir -p /opt/railmon && sudo git clone https://github.com/3dworldfreenow/trainfreedata.git /opt/railmon/app
```

```bash
sudo python3 -m venv /opt/railmon/venv && sudo /opt/railmon/venv/bin/pip install -r /opt/railmon/app/requirements.txt
```

```bash
sudo mkdir -p /opt/railmon/app/data && sudo chown -R railmon:railmon /opt/railmon
```

## 3. MySQL

```bash
sudo mysql_secure_installation
```

```bash
sudo mysql <<'SQL'
CREATE DATABASE railmon CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'railmon'@'127.0.0.1' IDENTIFIED BY 'CHANGE_DB_PASSWORD';
GRANT ALL PRIVILEGES ON railmon.* TO 'railmon'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
```

The schema is created by `flask railmon init-db` (step 6). `sql/schema.sql`
contains the identical DDL if you prefer to load it by hand:
`mysql -u railmon -p -h 127.0.0.1 railmon < sql/schema.sql` (then still run
`init-db` to seed sources and jobs).

## 4. Redis

```bash
sudo cp /opt/railmon/app/deploy/redis/railmon-redis.conf /etc/redis/railmon-redis.conf
```

Edit `/etc/redis/railmon-redis.conf` and set `requirepass`, then:

```bash
echo "include /etc/redis/railmon-redis.conf" | sudo tee -a /etc/redis/redis.conf && sudo systemctl restart redis-server
```

## 5. Configuration

```bash
sudo -u railmon cp /opt/railmon/app/.env.example /opt/railmon/app/.env && sudo chmod 600 /opt/railmon/app/.env
```

```bash
cd /opt/railmon/app && sudo -u railmon /opt/railmon/venv/bin/python -m railmon.genkeys
```

Paste the generated values into `.env`, and set `DATABASE_URL`, `REDIS_URL`
and `HTTP_USER_AGENT` (include a real contact address). **Back up
`DATA_ENCRYPTION_KEY`** — without it saved PNRs cannot be decrypted.

## 6. Initialise database, import timetable, create admin

All `flask` commands run from `/opt/railmon/app` as `railmon`:

```bash
cd /opt/railmon/app && sudo -u railmon /opt/railmon/venv/bin/flask --app railmon.wsgi railmon init-db
```

```bash
cd /opt/railmon/app && sudo -u railmon /opt/railmon/venv/bin/flask --app railmon.wsgi railmon import-datameet
```

(Downloads ~100 MB of CC0 open data into `data/datameet/` and imports it; takes
a few minutes. Re-running is safe: unchanged timetables are skipped, changed
ones become a new schedule version.)

```bash
cd /opt/railmon/app && sudo -u railmon /opt/railmon/venv/bin/flask --app railmon.wsgi railmon create-user admin --admin
```

Optional — mark trains/stations for live polling (used once a permitted live source is configured):

```bash
cd /opt/railmon/app && sudo -u railmon /opt/railmon/venv/bin/flask --app railmon.wsgi railmon monitor-train 12951 12301
```

Check what your server can reach:

```bash
cd /opt/railmon/app && sudo -u railmon /opt/railmon/venv/bin/flask --app railmon.wsgi railmon probe-sources
```

## 7. Services

```bash
sudo cp /opt/railmon/app/deploy/systemd/railmon-*.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now railmon-web railmon-scheduler
```

```bash
systemctl status railmon-web railmon-scheduler --no-pager
```

## 8. Apache

```bash
sudo a2enmod proxy proxy_http headers ssl rewrite expires
```

```bash
sudo cp /opt/railmon/app/deploy/apache/railmon.conf /etc/apache2/sites-available/railmon.conf
```

Edit `ServerName` (and certificate paths), then:

```bash
sudo a2ensite railmon && sudo a2dissite 000-default && sudo systemctl reload apache2
```

```bash
sudo certbot --apache -d railmon.example.com
```

Set `SESSION_COOKIE_SECURE=true` in `.env` once HTTPS works and restart `railmon-web`.

## 9. Verify

```bash
curl -s http://127.0.0.1:8000/api/v1/health
```

Open the site, log in, and check **Sources** and **Settings → Monitoring jobs**.

## Upgrades

```bash
cd /opt/railmon/app && sudo -u railmon git pull && sudo /opt/railmon/venv/bin/pip install -r requirements.txt
```

```bash
cd /opt/railmon/app && sudo -u railmon /opt/railmon/venv/bin/flask --app railmon.wsgi railmon init-db && sudo systemctl restart railmon-web railmon-scheduler
```

`init-db` only creates missing tables. For column changes between releases,
compare `sql/schema.sql` with your database and apply `ALTER TABLE`s.

## Logs

* Application/system log: dashboard **Settings → System logs** (`system_logs` table)
* Services: `journalctl -u railmon-web -u railmon-scheduler -f`
* Apache: `/var/log/apache2/railmon_*.log`
