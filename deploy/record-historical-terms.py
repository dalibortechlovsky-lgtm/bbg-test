#!/usr/bin/env python3
"""Record reviewed UBS-to-Bloomberg terms from a private JSON file."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.historical_terms import HistoricalTermsStore


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('database',type=Path)
    parser.add_argument('terms',type=Path)
    args=parser.parse_args()
    rows=json.loads(args.terms.read_text(encoding='utf-8'))
    print(json.dumps(HistoricalTermsStore(args.database).record(rows),sort_keys=True))


if __name__=='__main__':main()
