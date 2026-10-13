"""Provision the authorized single-server deployment. Never logs credential values."""
import base64
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'data'/'hetzner-deployment.json'


def api(path, method='GET', payload=None):
    token=os.environ.get('HETZNER_API_TOKEN')
    if not token:
        raise RuntimeError('HETZNER_API_TOKEN is not available')
    body=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request('https://api.hetzner.cloud/v1/'+path,data=body,method=method,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        error=json.loads(exc.read()).get('error',{})
        raise RuntimeError(f"Hetzner HTTP {exc.code}: {error.get('code')} - {error.get('message')}") from None


def write_state(state):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps(state,indent=2))
    os.chmod(STATE,0o600)


def wait_action(action_id):
    for attempt in range(60):
        action=api(f'actions/{action_id}')['action']
        if action['status']=='success':
            return
        if action['status']=='error':
            raise RuntimeError('Hetzner action failed: '+str(action.get('error',{}).get('code')))
        time.sleep(2)
    raise RuntimeError('Hetzner action still running; inspect stored action before retrying')


def user_data():
    buffer=io.BytesIO()
    files=list((ROOT/'src').glob('*.py')) + [ROOT/'web'/'index.html',ROOT/'Dockerfile',ROOT/'.dockerignore',
        ROOT/'deploy'/'compose.yaml',ROOT/'deploy'/'Caddyfile',ROOT/'bridge'/'bloomberg_bridge.py']
    with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
        for path in files:
            name=str(path.relative_to(ROOT))
            entry=tarfile.TarInfo(name)
            raw=path.read_bytes()
            entry.size=len(raw)
            entry.mode=0o644
            archive.addfile(entry,io.BytesIO(raw))
    config={'write_files':[
        {'path':'/opt/bbg-source.tar.gz','permissions':'0600','encoding':'b64',
         'content':base64.b64encode(buffer.getvalue()).decode()},
        {'path':'/root/bbg-bootstrap.sh','permissions':'0700','content':(ROOT/'deploy'/'bootstrap.sh').read_text()}],
        'runcmd':[['bash','/root/bbg-bootstrap.sh']]}
    text='#cloud-config\n'+json.dumps(config)
    if len(text.encode())>32768:
        raise RuntimeError('Cloud-init payload exceeds Hetzner limit')
    return text


def main():
    existing=api('servers?per_page=50')['servers']
    found=[s for s in existing if s['name']=='bbg-frontoffice']
    if found:
        server=found[0]
        state={'server_id':server['id'],'ip':server['public_net']['ipv4']['ip'],
               'hostname':server['public_net']['ipv4']['dns_ptr'],'status':server['status'],
               'backups_enabled':server.get('backup_window') is not None}
        write_state(state)
        if not state['backups_enabled']:
            backup=api(f"servers/{server['id']}/actions/enable_backup",'POST',{})
            wait_action(backup['action']['id'])
            state['backups_enabled']=True
            write_state(state)
        print(json.dumps(state))
        return
    if STATE.exists():
        raise RuntimeError('Prior deployment state exists but its server was not found; inspect before creating anything')
    types=api('server_types?per_page=50')['server_types']
    candidates=[]
    for t in types:
        if t['architecture']!='x86' or t['cores']<2 or t['memory']<4:
            continue
        for location in t.get('locations',[]):
            if location['name'] not in ('fsn1','nbg1','hel1') or not location.get('available'):
                continue
            price=next(p for p in t['prices'] if p['location']==location['name'])
            candidates.append((Decimal(price['price_monthly']['gross']),t['name'],location['name']))
    if not candidates:
        raise RuntimeError('No suitable EU server is available')
    price,kind,location=sorted(candidates,key=lambda x:(x[0],x[2]!='fsn1',x[2]))[0]
    pricing=api('pricing')['pricing']
    ip_pricing=next(p for p in pricing['primary_ips'] if p['type']=='ipv4')
    ip_price=Decimal(next(p for p in ip_pricing['prices'] if p['location']==location)['price_monthly']['gross'])
    cost=price*(1+Decimal(pricing['server_backup']['percentage'])/100)+ip_price
    if cost>Decimal('35'):
        raise RuntimeError('Available server exceeds the intended small-server budget; do not purchase')
    config=user_data()  # Prepare and size-check before any billable allocation.
    key=ROOT/'data'/'hetzner-ssh-ed25519'
    key.parent.mkdir(exist_ok=True)
    if not key.exists():
        subprocess.run(['ssh-keygen','-t','ed25519','-f',str(key),'-N','','-C','bbg-deployment'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    public=key.with_suffix('.pub').read_text().strip()
    keys=api('ssh_keys?per_page=50')['ssh_keys']
    matched=[k for k in keys if k['public_key'].strip()==public]
    ssh_id=matched[0]['id'] if matched else api('ssh_keys','POST',{'name':'bbg-deployment','public_key':public})['ssh_key']['id']
    firewalls=api('firewalls?per_page=50')['firewalls']
    matched=[f for f in firewalls if f['name']=='bbg-web-only']
    firewall_id=matched[0]['id'] if matched else api('firewalls','POST',{'name':'bbg-web-only','rules':[
        {'direction':'in','protocol':'tcp','port':'80','source_ips':['0.0.0.0/0','::/0'],'description':'HTTPS certificate validation and redirect'},
        {'direction':'in','protocol':'tcp','port':'443','source_ips':['0.0.0.0/0','::/0'],'description':'Authenticated dashboard'}]})['firewall']['id']
    response=api('servers','POST',{'name':'bbg-frontoffice','server_type':kind,'location':location,
        'image':'ubuntu-24.04','ssh_keys':[ssh_id],'user_data':config,
        'firewalls':[{'firewall':firewall_id}], 'labels':{'app':'bbg-frontoffice'},
        'public_net':{'enable_ipv4':True,'enable_ipv6':False},'start_after_create':True})
    server=response['server']
    state={'server_id':server['id'],'ip':server['public_net']['ipv4']['ip'],
           'hostname':server['public_net']['ipv4']['dns_ptr'],'server_type':kind,'location':location,
           'monthly_estimate_gross':str(cost),'currency':pricing['currency'],
           'backups_enabled':False,'create_action_id':response['action']['id']}
    write_state(state)
    wait_action(response['action']['id'])
    backup=api(f"servers/{server['id']}/actions/enable_backup",'POST',{})
    state['backup_action_id']=backup['action']['id']
    write_state(state)
    wait_action(backup['action']['id'])
    state['backups_enabled']=True
    write_state(state)
    print(json.dumps(state))


if __name__=='__main__':
    main()
