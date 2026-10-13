import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

def run(args, **kwargs):
    result=subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        print(result.stderr)
        raise RuntimeError('Deployment smoke command failed: '+args[1])
    return result.stdout.strip()

ROOT=Path(__file__).resolve().parents[1]
suffix=uuid.uuid4().hex[:10]
network='bbg-smoke-'+suffix
app='bbg-app-'+suffix
proxy='bbg-proxy-'+suffix
volume='bbg-data-'+suffix
containers=[]
try:
    run(['docker','network','create','--internal',network])
    run(['docker','volume','create',volume])
    password_hash=run(['docker','run','--rm','caddy:2.10','caddy','hash-password','--plaintext','synthetic-smoke-password'])
    run(['docker','run','-d','--name',app,'--network',network,'--network-alias','app',
         '--read-only','--tmpfs','/tmp','--cap-drop','ALL','--security-opt','no-new-privileges:true',
         '-e','PUBLIC_ORIGIN=https://dashboard.test','-v',volume+':/data','bbg-frontoffice:deployment-test'])
    containers.append(app)
    run(['docker','create','--name',proxy,'--network',network,'--network-alias','dashboard.test',
         '-e','DOMAIN=dashboard.test','-e','DASHBOARD_USER=test-user','-e','NO_PROXY=app,localhost,127.0.0.1',
         '-e','DASHBOARD_PASSWORD_HASH='+password_hash,'caddy:2.10'])
    containers.append(proxy)
    with tempfile.TemporaryDirectory() as directory:
        config=Path(directory)/'Caddyfile'
        config.write_text('{\n local_certs\n}\n'+(ROOT/'deploy'/'Caddyfile').read_text())
        run(['docker','cp',str(config),proxy+':/etc/caddy/Caddyfile'])
        run(['docker','start',proxy])
        root=Path(directory)/'root.crt'
        for _ in range(30):
            p=subprocess.run(['docker','cp',proxy+':/data/caddy/pki/authorities/local/root.crt',str(root)],capture_output=True)
            if p.returncode==0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Local test CA not created')
        run(['docker','exec','-i',app,'python','-c',"import sys; from pathlib import Path; Path('/tmp/root.crt').write_text(sys.stdin.read())"],input=root.read_text())
        import sys
        sys.path.insert(0,str(ROOT))
        sys.path.insert(0,str(ROOT/'tests'))
        from test_keytrader import export
        client='''
import base64,json,ssl,time
from urllib.request import Request,build_opener,ProxyHandler,HTTPSHandler,HTTPRedirectHandler
from urllib.error import HTTPError
context=ssl.create_default_context(cafile='/tmp/root.crt')
opener=build_opener(ProxyHandler({}),HTTPSHandler(context=context))
url='https://dashboard.test/api/status'
for _ in range(30):
 try:
  opener.open(url,timeout=3)
 except HTTPError as e:
  assert e.code==401,e.code
  break
 except Exception as exc:
  last=str(exc)
  time.sleep(.5)
else: raise RuntimeError('Proxy did not become ready: '+last)
auth='Basic '+base64.b64encode(b'test-user:synthetic-smoke-password').decode()
with opener.open(Request(url,headers={'Authorization':auth}),timeout=5) as r:
 s=json.load(r)
assert s['execution_enabled'] is False
assert s['positions']==[] and s['trades']==[]
print('HTTPS certificate trusted, anonymous access rejected, authenticated dashboard verified')
headers={'Authorization':auth,'Content-Type':'text/csv','X-Import-Format':'ubs-orders','Origin':'https://dashboard.test'}
with opener.open(Request('https://dashboard.test/api/import/orders', data=__ORDER_CONTENT__.encode(), headers=headers),timeout=5) as r:
 assert json.load(r)['added']==1
try:
 opener.open(Request('https://dashboard.test/api/import/orders', data=__ORDER_CONTENT__.encode(), headers=headers|{'Origin':'https://foreign.test'}),timeout=5)
except HTTPError as e:
 assert e.code==403,e.code
else: raise AssertionError('Foreign origin accepted')
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
try:
 build_opener(ProxyHandler({}),NoRedirect()).open('http://dashboard.test/api/status',timeout=5)
except HTTPError as e:
 assert e.code==308,e.code
 assert e.headers['Location']=='https://dashboard.test/api/status'
else: raise AssertionError('HTTP did not redirect')
print('Authenticated HTTPS import, origin rejection, and HTTP-to-HTTPS redirect verified')

manage={'Authorization':auth,'Content-Type':'application/json','X-Bridge-Action':'manage','Origin':'https://dashboard.test'}
with opener.open(Request('https://dashboard.test/api/bridge/token',data=json.dumps({'name':'Synthetic smoke bridge'}).encode(),headers=manage),timeout=5) as r:
 device=json.load(r)
from bridge.bloomberg_bridge import cloud_request
config=device['config']
watchlist=cloud_request(config,'watchlist_url')
assert 'IBM US Equity' in watchlist['securities']
from datetime import datetime,timezone
payload={'schema_version':1,'status':'connected','quotes':[{'security':'IBM US Equity','fieldData':{'PX_LAST':'123.45','CRNCY':'USD'},'observed_at':datetime.now(timezone.utc).isoformat()}]}
assert cloud_request(config,'endpoint',payload)['accepted']==1
assert cloud_request(config,'endpoint',payload)['ignored']==1
try:
 opener.open(Request(url,headers={'Authorization':'Bearer '+config['token']}),timeout=5)
except HTTPError as e: assert e.code==401
else: raise AssertionError('Device token accessed account data')
with opener.open(Request(url,headers={'Authorization':auth}),timeout=5) as r:
 state=json.load(r)
assert len(state['bloomberg_data']['quotes'])==1
assert config['token'] not in json.dumps(state)
with opener.open(Request('https://dashboard.test/api/bridge/revoke',data=json.dumps({'id':device['id']}).encode(),headers=manage),timeout=5) as r:
 assert json.load(r)['revoked']
try: cloud_request(config,'endpoint',payload)
except HTTPError as e: assert e.code==401
else: raise AssertionError('Revoked token accepted')
print('Windows uploader HTTPS contract, replay timestamps, token scope and revocation verified')

'''
        client=client.replace('__ORDER_CONTENT__',json.dumps(export()))
        print(run(['docker','exec','-i','-e','NO_PROXY=dashboard.test,app,localhost,127.0.0.1','-e','no_proxy=dashboard.test,app,localhost,127.0.0.1','-e','SSL_CERT_FILE=/tmp/root.crt',app,'python','-'],input=client))
        # Store synthetic order inside the non-root app container and check retention.
        import sys
        sys.path.insert(0,str(ROOT))
        sys.path.insert(0,str(ROOT/'tests'))
        from test_keytrader import export
        input_data=json.dumps(export(**{'Order number':'SECOND'}))
        persist="from src.storage import SnapshotStore; s=SnapshotStore('/data/frontoffice.sqlite3'); r=s.import_orders_content("+input_data+"); assert r['added']==1; print('Non-root database write verified')"
        print(run(['docker','exec',app,'python','-c',persist]))
        print(run(['docker','exec',app,'python','-m','src.backup','/data/frontoffice.sqlite3','/data/backups/smoke.sqlite3']))
        restored="from src.storage import SnapshotStore; assert len(SnapshotStore('/data/backups/smoke.sqlite3').orders())==2; print('Backup restoration verified')"
        print(run(['docker','exec',app,'python','-c',restored]))
        run(['docker','restart',app])
        retained="from src.storage import SnapshotStore; s=SnapshotStore('/data/frontoffice.sqlite3'); assert len(s.orders())==2; print('Persistent volume survived app restart')"
        print(run(['docker','exec',app,'python','-c',retained]))
except Exception:
    print(subprocess.run(['docker','logs',app],capture_output=True,text=True).stderr)
    print(subprocess.run(['docker','logs',proxy],capture_output=True,text=True).stderr[-5000:])
    raise
finally:
    for name in reversed(containers):
        subprocess.run(['docker','rm','-f',name],capture_output=True)
    subprocess.run(['docker','volume','rm',volume],capture_output=True)
    subprocess.run(['docker','network','rm',network],capture_output=True)
