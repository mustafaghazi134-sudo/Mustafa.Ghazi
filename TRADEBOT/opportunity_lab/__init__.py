"""TRADEBOT Opportunity Lab: an independent, PAPER-ONLY, four-agent Solana opportunity discovery experiment.

  Agent 1  scanner     discovers candidate tokens and records every observation with its source and time
  Agent 2  security    rejects dangerous or untradable tokens (fails closed when data is missing)
  Agent 3  analyst     deterministic strategies propose trades with costs, sizing and reasons
  Agent 4  executor    simulates entry, monitoring, exits and P&L after all estimated costs

Isolation from the main bot: own package, own config (`lab.toml`), own data directory (`data/lab/`), own SQLite ledger,
own halt file (`data/lab/LAB_HALT`). It never imports the wallet, signer, live adapter or submission switch, never
sends a transaction, and never writes to the main bot's files. It honours the main bot's `data/KILL` (read-only).
"""
