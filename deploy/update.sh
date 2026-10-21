#!/bin/bash
# Update source only. Keep the existing environment, database volumes and passwords.
set -Eeuo pipefail
: "${BBG_RELEASE_SHA:?Set BBG_RELEASE_SHA to the reviewed 40-character commit}"
[[ "$BBG_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid release commit'; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo 'Run from the server root console'; exit 1; }
app=/opt/bbg-test
[[ -d "$app" && ! -L "$app" && -f "$app/deploy/.env" ]] || { echo 'Existing deployment not found'; exit 1; }
exec 9>/run/bbg-update.lock
flock -n 9 || { echo 'Another update is running'; exit 1; }
umask 077
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/opt/bbg-backups/$stamp
install -d -m 0700 "$backup"
tmp=$(mktemp -d /opt/.bbg-update.XXXXXX)
changed=0
prior_release=development
rollback() {
    trap - ERR
    if [[ "$changed" == 1 ]]; then
        echo 'Update failed; restoring prior application source (database retained).'
        rm -rf "$app/src" "$app/web" "$app/bridge"
        tar -xzf "$backup/source.tar.gz" -C "$app"
        cd "$app/deploy"
        BBG_RELEASE_SHA="$prior_release" docker compose --env-file .env up -d --build --wait --wait-timeout 180 || true
        docker compose --env-file .env exec -T proxy caddy reload --config /etc/caddy/Caddyfile || true
    fi
    echo "Preserved backups: $backup"
    exit 1
}
trap rollback ERR
trap 'rm -rf "$tmp"' EXIT
curl --proto '=https' --tlsv1.2 -fsSL --max-time 120 \
    "https://codeload.github.com/dalibortechlovsky-lgtm/bbg-test/tar.gz/$BBG_RELEASE_SHA" -o "$tmp/release.tar.gz"
tar -xzf "$tmp/release.tar.gz" -C "$tmp"
release="$tmp/bbg-test-$BBG_RELEASE_SHA"
[[ -f "$release/src/quotes.py" && -f "$release/bridge/bloomberg_bridge.py" && -f "$release/deploy/Caddyfile" ]]
cd "$app/deploy"
# Capture only counts and snapshot identity; never print account data or credentials.
python3 - "$backup/before.json" <<'PY'
import base64,json,sys,urllib.request
from pathlib import Path
login=json.loads(Path('/root/bbg-dashboard-login.json').read_text())
auth='Basic '+base64.b64encode((login['username']+':'+login['password']).encode()).decode()
with urllib.request.urlopen(urllib.request.Request(login['url']+'/api/status',headers={'Authorization':auth}),timeout=15) as r:
    state=json.load(r)
Path(sys.argv[1]).write_text(json.dumps({'positions':len(state['positions']),'trades':len(state['trades']),
                                      'snapshot_id':(state.get('snapshot') or {}).get('id'),
                                      'performance_events':state.get('manual_performance_event_count',state.get('performance',{}).get('stored_event_count',0)),
                                      'ubs_activity_records':(state.get('activity') or {}).get('record_count',0),
                                      'ubs_activity_exports':(state.get('activity') or {}).get('export_count',0),
                                      'ptd_records':(state.get('ptd') or {}).get('stored_record_count',0),
                                      'ptd_corrections':len((state.get('ptd') or {}).get('corrections',[])),
                                      'risk_inputs':state.get('risk_inputs_count',0),
                                      'mars_snapshots':state.get('mars_snapshot_count',0),
                                      'accounting_sources':state.get('accounting_source_counts',{})}))
PY
prior_release=$(curl --proto '=https' --tlsv1.2 -fsS -D - -o /dev/null --max-time 15 "$(python3 -c "import json; print(json.load(open('/root/bbg-dashboard-login.json'))['url'])")/health" | awk 'tolower($1)=="x-app-release:" {gsub("\r", "", $2); print $2}')
prior_release=${prior_release:-development}
container=$(docker compose --env-file .env ps -q app)
docker compose --env-file .env exec -T app python -m src.backup /data/frontoffice.sqlite3 "/data/backups/update-$stamp.sqlite3"
docker cp "$container:/data/backups/update-$stamp.sqlite3" "$backup/database.sqlite3"
chmod 0600 "$backup/database.sqlite3"
files=(src web Dockerfile .dockerignore deploy/compose.yaml deploy/Caddyfile)
[[ ! -d "$app/bridge" ]] || files+=(bridge)
tar -czf "$backup/source.tar.gz" -C "$app" "${files[@]}"
# .env and all Docker volumes stay in place; no compose down/rebuild-server action.
changed=1
rm -rf "$app/src" "$app/web" "$app/bridge"
cp -a "$release/src" "$release/web" "$release/bridge" "$app/"
cp "$release/Dockerfile" "$release/.dockerignore" "$app/"
cp "$release/deploy/compose.yaml" "$release/deploy/Caddyfile" "$app/deploy/"
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build --wait --wait-timeout 180
docker compose --env-file .env exec -T proxy caddy reload --config /etc/caddy/Caddyfile
docker compose --env-file .env exec -T app python -c "import shutil; assert shutil.which('pdftotext'), 'PDF extractor unavailable'"
python3 - "$backup/before.json" "$BBG_RELEASE_SHA" <<'PY'
import base64,json,os,sys,time,urllib.request,urllib.error
from pathlib import Path
login=json.loads(Path('/root/bbg-dashboard-login.json').read_text())
auth='Basic '+base64.b64encode((login['username']+':'+login['password']).encode()).decode()
base=login['url']
before=json.loads(Path(sys.argv[1]).read_text())
def request(path,data=None,headers=None):
    body=json.dumps(data).encode() if data is not None else None
    values={'Authorization':auth,'Content-Type':'application/json','X-Bridge-Action':'manage','Origin':base}
    if headers is not None: values=headers
    with urllib.request.urlopen(urllib.request.Request(base+path,data=body,headers=values),timeout=15) as r:
        return json.load(r)
stage='request status';last_error='none'
for attempt in range(20):
    try:
        state=request('/api/status')
        stage='core retained counts'
        assert len(state['positions'])==before['positions'] and len(state['trades'])==before['trades']
        assert (state.get('snapshot') or {}).get('id')==before['snapshot_id']
        assert state['execution_enabled'] is False and 'bloomberg_data' in state
        assert 'analytics' in state
        assert state['performance']['method']=='average_cost'
        assert state['manual_performance_event_count']==before['performance_events']
        assert (state.get('activity') or {}).get('record_count',0)==before['ubs_activity_records']
        assert (state.get('activity') or {}).get('export_count',0)==before['ubs_activity_exports']
        assert (state.get('ptd') or {}).get('stored_record_count',0)==before['ptd_records']
        assert len((state.get('ptd') or {}).get('corrections',[]))==before['ptd_corrections']
        assert state['risk_inputs_count']==before['risk_inputs']
        assert state['mars_snapshot_count']==before['mars_snapshots']
        if state.get('mars'):
            assert state['mars']['calculation_policy']=='base_data_only'
        assert 'risk' in state and 'accounting' in state and 'snapshot_risk' in state
        stage='accounting source counts'
        for key,value in before['accounting_sources'].items():
            assert state['accounting_source_counts'][key]>=value
        stage='release header'
        with urllib.request.urlopen(base+'/health',timeout=15) as response:
            assert response.headers.get('X-App-Release')==sys.argv[2]
        if state.get('snapshot'):
            stage='portfolio analytics invariants'
            from decimal import Decimal
            analytics=state['analytics']
            assert analytics['position_count']==before['positions']
            assert Decimal(analytics['broker_market_value'])==Decimal(state['snapshot']['summary']['market_value'])
            assert analytics['daily_pnl'] is None and analytics['realized_pnl'] is None
        break
    except Exception as exc:
        last_error=type(exc).__name__+(': '+str(exc) if str(exc) else '')
        time.sleep(3)
else: raise RuntimeError('Updated dashboard readiness or data-preservation check failed at '+stage+' ('+last_error+')')
# Apply private owner confirmations supplied with this invocation, never in public source.
confirmation=os.environ.get('BBG_SETTLEMENTS_B64')
if confirmation:
    rows=json.loads(base64.b64decode(confirmation,validate=True))
    assert request('/api/accounting/settlements',rows,{'Authorization':auth,'Content-Type':'application/json',
        'X-Accounting-Action':'manage','Origin':base})['confirmed']==len(rows)
# Verify report downloads without importing any synthetic trading data.
if state.get('snapshot'):
    for path,expected in [('/api/reports/trader.html',b'Trading Overview'),('/api/reports/trader.csv',b'section,')]:
        with urllib.request.urlopen(urllib.request.Request(base+path,headers={'Authorization':auth}),timeout=15) as r:
            assert r.status==200 and expected in r.read()
if state.get('mars'):
    for path,expected in [('/api/reports/mars.html',b'MARS base data'),('/api/reports/mars.csv',b'valuation_date')]:
        with urllib.request.urlopen(urllib.request.Request(base+path,headers={'Authorization':auth}),timeout=15) as r:
            assert r.status==200 and expected in r.read()
# Exercise device auth using an empty starting heartbeat; never inject test prices.
device=request('/api/bridge/token',{'name':'Release verification (temporary)'})
headers={'Authorization':'Bearer '+device['config']['token'],'Content-Type':'application/json'}
try:
    assert request('/api/bridge/watchlist',headers=headers)['securities']
    assert request('/api/bridge/quotes',{'schema_version':1,'status':'starting','quotes':[]},headers)['accepted']==0
    assert request('/api/bridge/history',{'quotes':[]},headers)['accepted']==0
    try:
        request('/api/status',headers=headers)
    except urllib.error.HTTPError as exc:
        assert exc.code==401
    else: raise RuntimeError('Device token improperly accessed account data')
finally:
    assert request('/api/bridge/revoke',{'id':device['id']})['revoked']
Path('/root/bbg-update-status.json').write_text(json.dumps({'ready':True,'release':sys.argv[2],
    'positions':before['positions'],'trades':before['trades'],'bridge_endpoint_verified':True}))
print('Update verified: positions and orders retained; bridge authentication works. No test prices uploaded.')
PY
trap - ERR
echo "Update complete. Protected backup: $backup"
