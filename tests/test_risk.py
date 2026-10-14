import csv
from datetime import datetime,timedelta,timezone
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from src.positions import parse_positions
from tests.test_positions import sample
from src.risk import HEADERS, RiskStore, bsm, derivatives_risk


def term(snapshot,now,kind='european_call'):
    p=snapshot['positions'][-1]
    return {'position_id':p['id'],'kind':kind,'underlying':'TEST Equity','currency':'USD','multiplier':'100',
            'strike':'100','expiry':(now+timedelta(days=365)).isoformat(),'spot':'100','volatility':'0.2',
            'rate':'0.05','dividend_yield':'0','observed_at':(now-timedelta(seconds=10)).isoformat(),'verified':'yes'}


def content(rows):
    out=StringIO(); w=csv.DictWriter(out,HEADERS); w.writeheader(); w.writerows(rows); return out.getvalue()


class RiskTests(unittest.TestCase):
    def test_known_bsm_and_put_call_parity(self):
        c=bsm(100,100,1,.2,.05,0,True); p=bsm(100,100,1,.2,.05,0,False)
        self.assertAlmostEqual(c['price'],10.450583572,places=8)
        self.assertAlmostEqual(c['delta'],.636830651,places=8)
        self.assertAlmostEqual(c['gamma'],.018762017,places=8)
        self.assertAlmostEqual(c['vega'],.375240347,places=8)
        self.assertAlmostEqual(c['theta'],-.017572678,places=8)
        import math
        self.assertAlmostEqual(c['price']-p['price'],100-100*math.exp(-.05),places=8)
        # Check Greeks against numerical repricing, not just implementation constants.
        up=bsm(100.01,100,1,.2,.05,0,True)['price']; down=bsm(99.99,100,1,.2,.05,0,True)['price']
        self.assertAlmostEqual((up-down)/.02,c['delta'],places=6)
        self.assertAlmostEqual((up+down-2*c['price'])/.0001,c['gamma'],places=6)

    def test_signed_holdings_full_scenarios_and_stale_exclusion(self):
        s=parse_positions(sample()); now=datetime.now(timezone.utc); t=term(s,now)
        s['positions'][-1]['quantity_or_balance']='-2'
        r=derivatives_risk(s,[t],now)
        self.assertLess(float(r['rows'][0]['vega_1pp']),0)
        self.assertGreater(float(r['rows'][0]['scenarios']['spot_-10pct']),0)
        self.assertLess(float(r['rows'][0]['scenarios']['spot_+10pct']),0)
        self.assertIn('USD',r['totals'])
        stale=derivatives_risk(s,[t],now+timedelta(minutes=16))
        self.assertEqual(stale['totals'],{})
        self.assertEqual(stale['rows'][0]['status'],'stale')
        expired=derivatives_risk(s,[t],now+timedelta(days=366))
        self.assertEqual(expired['rows'][0]['status'],'expired; settlement required')
        self.assertFalse(expired['totals'])

    def test_future_is_not_asset_value_and_no_guessing(self):
        s=parse_positions(sample()); now=datetime.now(timezone.utc)
        r=derivatives_risk(s,[term(s,now,'future')],now)
        self.assertIsNone(r['rows'][0]['model_value'])
        self.assertEqual(float(r['rows'][0]['delta_cash']),20000)
        self.assertEqual(float(r['rows'][0]['scenarios']['spot_-10pct']),-2000)
        self.assertEqual(len(derivatives_risk(s,[],now)['uncovered']),1)
        s['positions'][-1]['quantity_or_balance']='1e1000'
        unsupported=derivatives_risk(s,[term(s,now)],now)
        self.assertFalse(unsupported['totals'])
        self.assertIn('Unsupported',unsupported['rows'][0]['status'])

    def test_verified_validation_and_atomic_updates(self):
        s=parse_positions(sample()); now=datetime.now(timezone.utc); good=term(s,now)
        with tempfile.TemporaryDirectory() as d:
            store=RiskStore(Path(d)/'db')
            store.import_content(content([good]),s)
            for overrides in [{'kind':'american_call'},{'verified':'no'},{'currency':'EUR'},
                              {'volatility':'25'},{'spot':'NaN'},{'position_id':'UNKNOWN'},
                              {'observed_at':'2026-10-10T10:00:00'},{'expiry':'2099-01-01T00:00:00Z'}]:
                with self.assertRaises(ValueError): store.import_content(content([good|overrides]),s)
                self.assertEqual(len(store.terms()),1)
            older=good|{'observed_at':(now-timedelta(hours=1)).isoformat()}
            with self.assertRaises(ValueError): store.import_content(content([older]),s)
            self.assertEqual(store.terms()[0]['spot'],'100')
