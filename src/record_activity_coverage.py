"""Record an explicit owner coverage statement, preserving a database backup."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import sqlite3
from .storage import SnapshotStore
from .activity_confirmations import ActivityConfirmationStore

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database');parser.add_argument('currency')
    parser.add_argument('start');parser.add_argument('end')
    parser.add_argument('kind',choices=['no_activity','no_further_transactions'])
    parser.add_argument('reason')
    args=parser.parse_args();path=Path(args.database)
    snapshot=SnapshotStore(path).latest()
    matches=[p for p in snapshot['positions'] if p['kind']=='cash_account' and p['currency']==args.currency.upper()]
    if len(matches)!=1:raise ValueError('Exactly one matching cash account required')
    backup=path.parent/'backups'/('coverage-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite3')
    backup.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as source,sqlite3.connect(backup) as target:source.backup(target)
    backup.chmod(0o600)
    row={'account':matches[0]['raw']['IBAN'],'currency':args.currency.upper(),
         'from':args.start,'until':args.end,'kind':args.kind,'reason':args.reason}
    print(ActivityConfirmationStore(path).confirm([row]))

if __name__=='__main__':main()

