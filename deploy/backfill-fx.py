"""Run on the deployment host to backfill missing FX from the official CNB feed.

No credentials, positions or account exports are sent to CNB. The existing app
stays on its private network; the owner-run host helper retrieves public rates.
"""
import argparse
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment',type=Path,default=Path('/opt/bbg-test/deploy'))
    args=parser.parse_args()
    command=['docker','compose','exec','-T','app','python','-c',
             "import json; from src.accounting import accounting_view; print(json.dumps(accounting_view('/data/frontoffice.sqlite3')['history_requests']))"]
    result=subprocess.run(command,cwd=args.deployment,text=True,capture_output=True,check=True)
    requests=json.loads(result.stdout)
    if not requests:
        print('No historical FX backfill needed.')
        return
    years={int(d[:4]) for r in requests for d in r['dates']}
    years.update(int(d[:4])-1 for r in requests for d in r['dates'] if d[5:7]=='01' and int(d[8:])<=7)
    documents=[]
    for year in sorted(years):
        url=('https://www.cnb.cz/en/financial_markets/foreign_exchange_market/'
             'exchange_rate_fixing/year.txt?year='+str(year))
        with urlopen(url,timeout=30) as response:
            content=response.read(1024*1024+1)
        if len(content)>1024*1024:raise ValueError('CNB source exceeds size limit')
        documents.append({'year':year,'url':url,'text':content.decode('utf-8-sig')})
    subprocess.run(['docker','compose','exec','-T','app','python','-m','src.official_fx'],
                   cwd=args.deployment,input=json.dumps({'documents':documents}),text=True,check=True)


if __name__=='__main__':main()
