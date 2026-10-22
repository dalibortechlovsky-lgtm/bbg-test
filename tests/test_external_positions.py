import csv
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from src.external_positions import HEADERS,ExternalPositionStore,parse_external_positions,template
from src.server import create_server


def sample(value='1000000',institution='LGT',portfolio='A.S.26',as_of='2026-10-22'):
    out=StringIO();writer=csv.writer(out,lineterminator='\n');writer.writerow(HEADERS)
    writer.writerow([institution,portfolio,as_of,'deposit','USD deposit','USD',value,value,'0',''])
    return out.getvalue()


class ExternalPositionTests(unittest.TestCase):
    def test_strict_snapshot_scope_and_values(self):
        parsed=parse_external_positions(sample())
        self.assertEqual(parsed['institution'],'LGT')
        self.assertEqual(parsed['positions'][0]['market_value_native'],'1000000')
        bad=[sample().replace('market_value_native','wrong'),sample().replace('1000000','NaN'),
             sample(as_of='2999-01-01'),sample().replace('USD,1000000','usd,1000000'),
             sample()+sample(institution='OTHER').split('\n',1)[1]]
        for content in bad:
            with self.subTest(content=content[-100:]):
                with self.assertRaises(ValueError):parse_external_positions(content)

    def test_append_only_storage_and_aum_fx_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ExternalPositionStore(Path(directory)/'db.sqlite3')
            first=store.import_content(sample());self.assertFalse(first['already_imported'])
            self.assertTrue(store.import_content(sample())['already_imported'])
            quotes={'quotes':[{'security':'USDCZK Curncy','price':'21.5','currency':'CZK',
                'observed_at':'2026-10-22T09:00:00+00:00','stale':False}]}
            view=store.view(None,quotes)
            self.assertEqual(view['aum_czk'],'21500000.0')
            self.assertEqual(view['positions'][0]['fx_source'],'Bloomberg recent FX')
            ubs={'as_of':'2026-10-14','exchange_rates':{'USD/CZK':'21'}}
            fallback=store.view(ubs,{'quotes':[]})
            self.assertEqual(fallback['aum_czk'],'21000000')
            self.assertEqual(fallback['positions'][0]['fx_source'],'UBS snapshot FX fallback')
            self.assertIsNone(store.view(None,{'quotes':[]})['aum_czk'])

    def test_http_template_import_security_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            server=create_server(0,Path(directory)/'db.sqlite3')
            threading.Thread(target=server.serve_forever,daemon=True).start();root=f'http://127.0.0.1:{server.server_port}'
            headers={'Content-Type':'text/csv','X-Import-Format':'other-bank-positions'}
            try:
                with urlopen(root+'/api/templates/external-positions.csv') as response:
                    self.assertEqual(next(csv.reader(StringIO(response.read().decode('utf-8-sig')))),HEADERS)
                with urlopen(Request(root+'/api/import/external-positions',data=sample().encode(),headers=headers)) as response:
                    self.assertEqual(json.load(response)['count'],1)
                with urlopen(root+'/api/status') as response:data=json.load(response)
                self.assertEqual(data['external_positions']['positions'][0]['institution'],'LGT')
                self.assertIsNone(data['aum']['consolidated_czk'])
                self.assertEqual(data['aum']['valuation_dates'],['2026-10-22'])
                for override,code in [({'Origin':'https://untrusted.example'},403),
                                      ({'X-Import-Format':'wrong'},403),({'Content-Type':'text/plain'},415)]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(Request(root+'/api/import/external-positions',data=sample().encode(),headers=headers|override))
                    self.assertEqual(error.exception.code,code)
            finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
