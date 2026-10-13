#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose-v2 python3-bcrypt curl ca-certificates
systemctl enable --now docker
install -d -m 0755 /opt/bbg-test
# The archive contains application source only, never account exports or API tokens.
tar --no-same-owner -xzf /opt/bbg-source.tar.gz -C /opt/bbg-test
python3 - <<'PY'
import bcrypt,json,os,secrets,socket,urllib.request
from pathlib import Path
ip=urllib.request.urlopen('http://169.254.169.254/hetzner/v1/metadata/public-ipv4',timeout=10).read().decode().strip()
host=socket.gethostbyaddr(ip)[0].rstrip('.')
if not host.endswith('.clients.your-server.de') or ip not in socket.gethostbyname_ex(host)[2]:
    raise RuntimeError('Provider hostname does not resolve to this server')
password=secrets.token_urlsafe(32)
hash_value=bcrypt.hashpw(password.encode(),bcrypt.gensalt()).decode()
env=Path('/opt/bbg-test/deploy/.env')
env.write_text("DOMAIN="+host+"\nDASHBOARD_USER=trader\nDASHBOARD_PASSWORD_HASH='"+hash_value+"'\n")
os.chmod(env,0o600)
credentials=Path('/root/bbg-dashboard-login.json')
credentials.write_text(json.dumps({'url':'https://'+host,'username':'trader','password':password},indent=2))
os.chmod(credentials,0o600)
PY
cd /opt/bbg-test/deploy
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build
# Verify with system CA trust and credentials held in memory, never in CLI arguments.
python3 - <<'PY'
import base64,json,time,urllib.request
from pathlib import Path
login=json.loads(Path('/root/bbg-dashboard-login.json').read_text())
auth='Basic '+base64.b64encode((login['username']+':'+login['password']).encode()).decode()
for attempt in range(60):
    try:
        req=urllib.request.Request(login['url']+'/api/status',headers={'Authorization':auth})
        with urllib.request.urlopen(req,timeout=10) as response:
            state=json.load(response)
        assert state['execution_enabled'] is False
        Path('/root/bbg-deployment-status.json').write_text(json.dumps({'ready':True,'url':login['url']}))
        print('Authenticated HTTPS readiness check passed')
        break
    except Exception:
        time.sleep(10)
else:
    raise RuntimeError('Authenticated HTTPS readiness check did not pass; inspect cloud-init logs')
PY
