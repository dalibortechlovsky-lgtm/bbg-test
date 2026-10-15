# Proposed Hetzner deployment

Status: server created on 13 October 2026; see DEPLOYMENT.md for resource IDs,
price and outstanding public readiness/migration checks. No custom domain purchased.
Proposed domain: desk.dalibortrading.com, subject to availability and registration.

## Required account access

The account owner must open a Hetzner Cloud account, complete its verification and
billing, and create a project API token with read/write access. Enter the token
securely as HETZNER_API_TOKEN in the environment settings; never paste it into chat.
The token is scoped to HTTPS requests to api.hetzner.cloud. Account creation,
billing verification, and domain registration are not available through the tools
in this chat. There is no purchase without a billing account and usable access.

Original selection plan: the smallest available x86 EU instance with at least 2 vCPU and
4 GiB RAM, Ubuntu 24.04, IPv4, and provider backups enabled. Verify current catalog,
capacity and monthly price including IPv4/backups before creating it. The current API quote and chosen available server are recorded in DEPLOYMENT.md. Do not claim a server exists based on
this proposed specification. Publish only after the authenticated deployment and
restoration checks below pass.

## Server preparation

Use the chosen account/project. Create an SSH key for deployment; keep the private
key outside Git and restrict its permissions. Restrict SSH to the administration
source address. Allow TCP 80/443 publicly; do not expose 8000, SQLite, Docker's API,
or Bloomberg port 8194. Install Docker Engine and the Compose plugin using Docker's
official Ubuntu repository instructions, preserving signature and TLS verification:
https://docs.docker.com/engine/install/ubuntu/

Transfer a source-only release (src/, web/, Dockerfile, .dockerignore, deploy/) to
/opt/bbg-test. Do not embed API keys, raw bank exports, or personal data in the
container image. The default deployment starts with an empty database. Transfer
an existing database only through authenticated SSH/SCP and only after making a
consistent backup using Python sqlite3.Connection.backup, not a live file copy.
Register the domain with the user's registrar and set its DNS A record to the
server's IPv4. Add AAAA only if IPv6 connectivity/firewall have been configured.

## Configure authentication and start

From /opt/bbg-test/deploy copy .env.example to .env, set DOMAIN and DASHBOARD_USER,
and set a strong password hash. Generate the bcrypt hash interactively so the
password is not exposed in command-line arguments or shell history:

```sh
docker run --rm -it caddy:2.10 caddy hash-password
```

Paste the hash into DASHBOARD_PASSWORD_HASH in .env, retaining single quotes so
literal dollar signs are preserved. Set .env permissions to 600. .env is excluded
from Git. Do not use the example placeholder hash or share the actual password.

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build
```

Caddy obtains and renews HTTPS certificates once domain/DNS point to this server.
All dashboard pages and data/import API endpoints require HTTP Basic authentication
over HTTPS. The non-sensitive /health endpoint is public for readiness checks. The app has no public port and sits on an internal Docker network. Sign-in
uses the browser's username/password prompt, not an application account system.
This initial deployment does not provide MFA, multiple users, or order submission.
The SQLite database and Caddy certificate state use persistent Docker volumes.
Never use `docker compose down -v` on a server with retained user data.

## Readiness and backup

Verify unauthenticated HTTPS requests return 401; HTTP redirects to HTTPS. Verify
an authenticated request returns /api/status with execution_enabled=false. Use an
interactive password prompt, never a password literal in the command:

```sh
curl -u YOUR_USERNAME https://YOUR_DOMAIN/api/status
```

Test positions and order imports, then restart containers and verify retained
counts/date/quotes. Prices and bridge transport are not part of this deployment:
the Windows bridge still writes a local file and has no outbound cloud transport.

Create consistent SQLite backups using the included backup utility; each destination
must be a new file, and the utility integrity-checks the result:

```sh
docker compose exec -T app python -m src.backup /data/frontoffice.sqlite3 /data/backups/backup-YYYYMMDD.sqlite3
```

Copy the resulting backup to protected off-server storage. Test
restoring into a separate volume and verify row counts and reported totals before
relying on a backup. Provider backups supplement this procedure; none is configured
until a provider server actually exists. Keep backups out of Git and public paths.

For expiries or margin settlements already confirmed by the portfolio owner,
`record-expiries.py` provides a console repair without quoted Python. Download
the reviewed utility to the host's `/root/record-expiries.py`, then run:

```sh
docker compose exec --no-tty app python - --settled-by 2026-01-31 --currency usd us0000000001 < /root/record-expiries.py
```

Supply the actual cutoff, settlement currency and confirmed security references.
Lowercase ISINs and bare numeric Valors are accepted. The utility checks retained
executions and the latest UBS holdings, creates a protected consistent backup,
then records lifecycle annotations. Repeated runs retain existing audit reasons.
The utility is sent through standard input, which also works with the production
container's read-only filesystem. It adds no closing trades, prices or settlement
amounts. It requires no rebuild or restart and does not resolve unrelated
accounting reconciliation items.

For application updates, back up the database, update the source release, rebuild
and restart with Compose. Keep the prior release available for rollback. Restore
only to a new volume during tests; do not overwrite the user's working database.


## Local deployment validation

With Docker running, from the checkout root:

```sh
docker --config /tmp/bbg-docker-config build -t bbg-frontoffice:deployment-test .
python deploy/smoke_test.py
```

The smoke test creates temporary containers, an internal network and a synthetic
SQLite volume; it removes only those resources afterward. It uses Caddy's local
CA and explicitly trusts that CA in its HTTPS client, preserving certificate
verification. The production Caddyfile uses public automatic HTTPS instead. It
checks anonymous rejection, authenticated dashboard/import, foreign-origin
rejection, HTTP redirection, non-root writes, backup restoration and restart
persistence. It never uses the user's bank exports or database. This is local
validation, not evidence that a public domain or Hetzner deployment exists.
