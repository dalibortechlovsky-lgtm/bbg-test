from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.history import HistoryStore
from src.official_fx import cnb_url,parse_documents,import_documents


class OfficialFxTests(unittest.TestCase):
    def setUp(self):
        self.documents=[{'year':2026,'url':cnb_url(2026),
                         'text':'Date|1 USD|1 EUR|100 JPY\n02.01.2026|20.5|24.5|15\n05.01.2026|20.6|24.6|16\n'}]
        self.requests=[{'security':'USDCZK Curncy','dates':['2026-01-03','2026-01-05']},
                       {'security':'JPYCZK Curncy','dates':['2026-01-05']}]

    def test_dates_and_units_are_explicit_and_never_use_a_later_fixing(self):
        rows=parse_documents(self.documents,self.requests)
        self.assertEqual([r['price'] for r in rows],['20.5','20.6','0.16'])
        self.assertIn('fixing 2026-01-02',rows[0]['source'])
        self.assertIn('SHA256',rows[0]['source'])
        for day in ['2026-01-01','2026-10-14']:
            with self.assertRaisesRegex(ValueError,'unavailable'):
                parse_documents(self.documents,[{'security':'USDCZK Curncy','dates':[day]}])

    def test_wrong_source_or_layout_is_rejected(self):
        for change in [{'url':'https://example.com/rates'},
                       {'text':'<html>Source unavailable</html>'},
                       {'text':self.documents[0]['text'].replace('1 EUR','1 USD')},
                       {'text':self.documents[0]['text'].replace('20.5','NaN')}]:
            with self.assertRaises((ValueError,ArithmeticError)):
                parse_documents([self.documents[0]|change],self.requests)

    def test_import_preserves_source_and_backup_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3'
            with sqlite3.connect(path) as db:db.execute('CREATE TABLE retained (value TEXT)')
            result=import_documents(path,self.documents,self.requests)
            self.assertEqual(result['accepted'],3)
            self.assertEqual(Path(result['backup']).stat().st_mode&0o777,0o600)
            self.assertEqual(HistoryStore(path).rates()['USDCZK Curncy','2026-01-03'],Decimal('20.5'))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT content FROM fx_source_documents').fetchone()[0],self.documents[0]['text'])
            self.assertEqual(import_documents(path,self.documents,self.requests)['accepted'],0)
            with self.assertRaises(ValueError):
                import_documents(path,[self.documents[0]|{'text':self.documents[0]['text'].replace('20.5','21.5')}],self.requests)
            self.assertEqual(len(HistoryStore(path).rates()),3)
            with self.assertRaises(ValueError):
                import_documents(path,self.documents,[{'security':'USDCZK Curncy','dates':['2026-10-14']}])
            self.assertEqual(len(HistoryStore(path).rates()),3)


if __name__=='__main__':unittest.main()
