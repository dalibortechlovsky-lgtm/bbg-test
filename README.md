# Front Office Trade Control

Initial control-layer prototype for the workflow:

1. UBS KeyTrader execution
2. Bloomberg chat or KeyTrader EOD ingestion
3. Canonical trade ledger
4. Bloomberg MARS/PTT booking adapter
5. UBS positions reconciliation
6. Exceptions-only weekly control

## Current scope
- Canonical trade schema
- Bloomberg-style chat parser
- Source/status and deduplication logic
- KeyTrader CSV adapter scaffold
- Position reconciliation scaffold

External Bloomberg/UBS connectivity is intentionally behind adapters until real schemas/API access are available.
