from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.activity_confirmations import ActivityConfirmationStore,applicable
from src.accounting import calculate_accounting
from src.ubs_activity import ActivityStore
from src.desk_report import desk_csv,desk_html
from test_accounting import snapshot,STATEMENT,activity,IBAN
from src.baseline import parse_statement_text
from test_ubs_activity import account_csv,posting


def confirmation(**changes):
    return {'account':IBAN,'currency':'USD','from':'2026-01-03','until':'2026-10-14',
            'kind':'no_further_transactions','reason':'Owner confirmed no further transactions in the reporting period.'}|changes


class ActivityConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'db.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE snapshots (id TEXT,as_of TEXT,exported_at TEXT,payload TEXT)')
            db.execute('INSERT INTO snapshots VALUES (?,?,?,?)',('synthetic','2026-10-14','2026-10-15',json.dumps(snapshot())))
        ActivityStore(self.path).import_content(account_csv([posting('-10')]))

    def test_idempotent_owner_audit_preserves_bank_records_and_rejects_conflicts(self):
        before=ActivityStore(self.path).state()
        store=ActivityConfirmationStore(self.path);row=confirmation()
        self.assertEqual(store.confirm([row]),{'confirmed':1,'existing':0})
        self.assertEqual(store.confirm([row]),{'confirmed':0,'existing':1})
        self.assertEqual(store.all(),[row])
        self.assertEqual(ActivityStore(self.path).state(),before)
        with self.assertRaises(ValueError):store.confirm([row|{'reason':'Changed owner statement'}])
        with self.assertRaises(ValueError):store.confirm([row|{'kind':'no_activity'}])
        with self.assertRaises(ValueError):store.confirm([row|{'account':'CH0000000000000000099'}])

    def test_later_bank_activity_invalidates_owner_statement_without_deleting_audit(self):
        store=ActivityConfirmationStore(self.path);store.confirm([confirmation()])
        later=posting('-2',number='LATER',**{'Trade date':'2026-01-06','Booking date':'2026-01-08','Value date':'2026-01-08'})
        ActivityStore(self.path).import_content(account_csv([posting('-10'),later]))
        valid,issues=applicable(store.all(),ActivityStore(self.path).view()['ledger'],'2026-01-01','2026-10-14')
        self.assertEqual(valid,[])
        self.assertTrue(issues)
        self.assertEqual(len(store.all()),1)

    def test_statement_completes_only_the_named_account_and_period(self):
        baseline=parse_statement_text(STATEMENT);close=snapshot();view=activity([],until='2026-01-02')
        missing=calculate_accounting(close,baseline,view)
        self.assertFalse(missing['portfolio_pnl_verified'])
        owner=confirmation()
        result=calculate_accounting(close,baseline,view,activity_confirmations=[owner])
        self.assertTrue(result['portfolio_pnl_verified'])
        self.assertTrue(result['coverage'][0]['owner_confirmed'])
        self.assertFalse(result['coverage'][0]['bank_complete'])
        self.assertEqual(result['coverage'][0]['until'],'2026-01-02')
        self.assertIn('owner-confirmed',result['status'])
        self.assertIn(b'OWNER ACTIVITY CONFIRMATION',desk_csv({'accounting':result}))
        self.assertIn(b'Owner-confirmed coverage',desk_html({'accounting':result}))
        for different in [owner|{'until':'2026-10-13'},owner|{'account':'CH0000000000000000099'}]:
            self.assertFalse(calculate_accounting(close,baseline,view,activity_confirmations=[different])['portfolio_pnl_verified'])

    def test_zero_chf_account_needs_an_explicit_owner_statement_and_matching_balances(self):
        account='CH0000000000000000004'
        baseline=parse_statement_text(STATEMENT);close=snapshot();view=activity([])
        baseline['positions'].append({'key':None,'kind':'cash_account','currency':'CHF','quantity':'0',
                                     'market_value_native':'0','accrued_native':'0','iban':account,'description':'Synthetic CHF account'})
        close['positions'].append({'id':'chf','kind':'cash_account','group':'Liquidity - Accounts','currency':'CHF',
                                   'quantity_or_balance':'0','market_value':'0','description':'Synthetic CHF account',
                                   'market_price':None,'raw':{'IBAN':account}})
        self.assertFalse(calculate_accounting(close,baseline,view)['portfolio_pnl_verified'])
        row=confirmation(account=account,currency='CHF',**{'from':'2026-01-01','kind':'no_activity'})
        result=calculate_accounting(close,baseline,view,activity_confirmations=[row])
        self.assertTrue(result['portfolio_pnl_verified'])
        self.assertEqual(result['issues'],[])
        self.assertEqual(len(view['accounts']),1)
        self.assertEqual(result['coverage'][-1]['source'],'Owner confirmed no activity')
        changed=deepcopy(close);changed['positions'][-1]['quantity_or_balance']='1'
        self.assertFalse(calculate_accounting(changed,baseline,view,activity_confirmations=[row])['portfolio_pnl_verified'])


if __name__=='__main__':unittest.main()
