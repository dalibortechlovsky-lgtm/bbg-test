# Hetzner deployment state — 13 October 2026

Created under the user's explicit purchase/deploy authorization:

- Server: bbg-frontoffice, ID 170060589
- Type/location: CPX22 (2 vCPU, 4 GiB), fsn1/Falkenstein, Germany
- IPv4: 2.31.27.153
- Provider hostname: static.153.27.31.2.clients.your-server.de
- Approximate monthly cap: EUR 28.90 gross for server, IPv4 and backups;
  API quote includes 21% VAT. Excess usage is separate.
- Creation action 661227402254187 succeeded; backup action 661227402254310
  succeeded. Server API reports running and provider backups enabled.
- Firewall allows TCP 80 and 443. Port 22 and application port 8000 are not public.

Cloud-init installs the source-only dashboard and Caddy, generates a random
single-user dashboard password, and runs an authenticated HTTPS self-check. Login
information is stored only at /root/bbg-dashboard-login.json on the server, with
mode 0600; the username is trader. Do not print its contents into chat or agent
logs. The Hetzner account owner can use the provider Console to retrieve it
privately; if needed, use the provider's Reset root password action for console
login, keep that password private, then view the dashboard login file there.
Do not open SSH broadly as a shortcut. The SSH private deployment key is retained
under local data/, excluded from Git; do not print or share it in chat.

Readiness has not yet been independently confirmed on the public hostname. The
first HTTPS health request was denied by this workspace's destination policy
(CONNECT 403), not by Hetzner authentication. The exact hostname has been saved in
the environment network draft. After the owner saves/publishes it, retry /health
with normal proxy/TLS verification and check /api/status anonymously returns 401.
Bootstrap self-check outcome is recorded in /root/bbg-deployment-status.json; the
owner can inspect it through Console without exposing the separate login file.

The existing UBS data remains in the Codex workspace. No account exports or bank
database were sent in cloud-init or included in the image. Import/migration and
remote authenticated dashboard validation remain outstanding. Bloomberg's Windows
probe worked, but the persistent bridge still has no cloud upload connection.
The proposed custom domain desk.dalibortrading.com is not registered.

Do not create another server to recover this deployment. Inspect the existing
server, protected boot logs and saved local data/hetzner-deployment.json first.
Do not rebuild or delete it without preserving any new server-side user data.


## Public verification completed — 13 October 2026

After network access was published, normal TLS-verified requests returned:
- GET https://static.153.27.31.2.clients.your-server.de/health: 200, status ok,
  execution_enabled false.
- Anonymous GET /api/status: 401 Unauthorized.
- Hetzner server API: running, provider backups enabled.

This supersedes the earlier public-readiness block. The hosted application is
reachable and its data endpoint is protected. Owner sign-in, authenticated remote
UI validation and UBS data migration remain outstanding. Credentials stay only
in the protected server login file; do not print them in chat. Bloomberg upload
and ChatGPT tool integration are still absent.
