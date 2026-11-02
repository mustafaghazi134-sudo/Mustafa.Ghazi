# Opportunity Lab: deployment (Windows PC and Linux VPS)

The lab is paper-only and needs no wallet, no private key and no paid service. It runs beside the main bot without
touching it: separate process, `data/lab/` folder and ledger. It stops opening paper trades when the main bot's
`data/KILL` exists.

**Not yet tested on your PC or a VPS.** The build environment could not reach DexScreener, GeckoTerminal, Jupiter or
Solana RPC, so the live data path has been tested only against recorded responses. The first run on your machine is
the real test of those connections.

## 1. Windows PC

From the `TRADEBOT` folder, in PowerShell, with the existing virtual environment active:

1. Update the code and install the lab's extra dependencies. pandas and pyarrow are only needed for the real-data study.

   ```bash
   git pull
   ```

   ```bash
   pip install -e ".[research]" pyarrow
   ```

2. Check that everything passes.

   ```bash
   python -m pytest -q tests/test_opportunity_lab.py
   ```

3. Create the configuration. The defaults are sensible; read the comments.

   ```bash
   copy lab.toml.example lab.toml
   ```

4. Run a short read-only scan. It discovers tokens, runs the security agent and records proposals, but opens no paper
   trade. Look for `http={}` (no errors) and the rate-limit counters in the output.

   ```bash
   python -m opportunity_lab scan --minutes 10
   ```

5. Run the full paper loop. It runs until you press Ctrl+C; the state survives restarts.

   ```bash
   python -m opportunity_lab run --source live
   ```

6. In a second window, check progress:

   ```bash
   python -m opportunity_lab status
   ```

   ```bash
   python -m opportunity_lab report
   ```

To run it unattended, use Task Scheduler with "At log on" and "Restart on failure". Point it at
`python -m opportunity_lab run --source live`, with the start-in folder set to `TRADEBOT`. The lab refuses to start a
second copy (`data/lab/lab.lock`).

## 2. Linux VPS (24/7)

**Requirements:**

- 1 vCPU and 1 GB RAM are enough. The loop is I/O-bound at about 1 request/second.
- About 1 GB per month of disk for the ledger at the default 30-second poll.
- Outbound HTTPS to: api.dexscreener.com, api.geckoterminal.com, api.jup.ag, and your Solana RPC
  (api.mainnet-beta.solana.com, or a Helius URL).
- Python 3.11 or newer.
- No inbound ports.
- A Helius free key in `HELIUS_API_KEY` is recommended: the public RPC allows 40 requests per 10 seconds per method
  and is "not for production".

**Install:**

```bash
git clone https://github.com/mustafaghazi134-sudo/Mustafa.Ghazi.git && cd Mustafa.Ghazi/TRADEBOT
```

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[research]" pyarrow
```

```bash
cp lab.toml.example lab.toml && python -m pytest -q tests/test_opportunity_lab.py
```

**systemd unit** `/etc/systemd/system/tradebot-lab.service`. Run it as an unprivileged user, never root.

```ini
[Unit]
Description=TRADEBOT Opportunity Lab (paper only)
After=network-online.target

[Service]
User=tradebot
WorkingDirectory=/home/tradebot/Mustafa.Ghazi/TRADEBOT
ExecStart=/home/tradebot/Mustafa.Ghazi/TRADEBOT/.venv/bin/python -m opportunity_lab run --source live
Restart=always
RestartSec=30
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/tradebot/Mustafa.Ghazi/TRADEBOT/data/lab

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tradebot-lab && journalctl -u tradebot-lab -f
```

## 3. Monitor

- `python -m opportunity_lab status` shows paper cash, open positions, pending intents, the halt, and record counts.
- `python -m opportunity_lab report` shows net results after costs, the funnel, rejection reasons, exits, the naive
  benchmark and the AI shadow comparison. Add `--json` for the full structure.
- `data/lab/lab_events.jsonl` has one line per cycle: tracked tokens, opportunities, entries, exits, cash, exposure
  and rate-limit counters. Watch `rate_limited_429` and `in_backoff_sec`.
- The ledger `events` table records `source_error`, `entry_rejected`, `entry_failed`, `unsellable`, `halt` and
  `recovered`.

## 4. Stop and recover

- **Stop new paper entries:**

  ```bash
  python -m opportunity_lab halt --reason "<why>"
  ```

- **Stop and exit every paper position at the next cycle:**

  ```bash
  python -m opportunity_lab halt --flatten --reason "<why>"
  ```

- **Resume:**

  ```bash
  python -m opportunity_lab resume
  ```

- **The main bot's kill switch** (`tradebot kill`, or `data/KILL`) also stops new lab entries.
- **After a crash or reboot,** just start the lab again. Open positions and cash reload from `data/lab/lab.sqlite`;
  entries interrupted before their fill are abandoned and their reserved cash released; no paper order is duplicated.
- **To start a fresh experiment,** move the old `data/lab/` folder elsewhere rather than deleting it. Its snapshots
  are the dataset for later replay tests.

## 5. Optional AI shadow scoring (costs money; off by default)

1. Set `grok_enabled = true` or `claude_enabled = true` in `[ai]`, with `max_calls_per_day` and
   `max_spend_usd_per_day`.
2. Put `XAI_API_KEY` or `ANTHROPIC_API_KEY` in the environment, never in `lab.toml`.
3. Scores are stored on each proposal. `report` compares the trades the AI liked with all trades after at least 30
   scored trades. Scores never change a decision.

## 6. What remains unverified until it runs on your machine

- Live response shapes of DexScreener, GeckoTerminal and Jupiter Tokens v2 / `/order`. The parsers follow the
  documented fields and are tested on recorded examples.
- Real 429 frequency at these budgets.
- Token-2022 extension names as returned by your RPC.
- Windows Task Scheduler and VPS service behaviour.
