# Paper trading runner (GitHub Actions)

The existing 4h engine (`tradebot run --paper`) runs on a GitHub-hosted runner with live market data and **simulated
funds**. It was set up because the Claude cloud session cannot reach any market-data host and no PC was connected.
Nothing here can trade real money:

- `TRADEBOT_FORCE_PAPER=1` is set;
- the config is `mode = "paper"`;
- no keys or secrets exist in the workflow;
- the live adapter's build switch is off.

## What runs

| Item | Setting |
|---|---|
| Market | BTC, ETH and SOL spot (Solana: cbBTC, WETH, SOL against USDC), long or flat |
| Data | Binance spot 4h candles via `data-api.binance.vision`; Jupiter Price v3 and Tokens v2 (keyless) with DexScreener fallback |
| Strategies | **tsmom** (primary) and **momentum** (secondary) |
| Fills | Simulated: 30 bps fee + 15 bps slippage model per side (Jupiter quotes are used only when a key is configured; none in CI) |
| Capital | $1,000 paper |
| Risk limits | Unchanged from `config.toml`; see the Risk section below |
| Claude | Off: no research, no vetting, parameters frozen |

**tsmom:** enter when 30-day momentum is above zero and price is above the 100-day EMA, checked daily on 4h candles.
Exit when either condition fails, or at a 25% disaster stop.

**momentum:** enter on an EMA 12/26 cross while price is above the 100-period EMA. Exit at a 2.5 ATR stop or on the
reverse cross; no take-profit.

**Risk:**

- 10% of equity per position, 50% total exposure, at most 5 positions.
- Orders between $5 and $500.
- A 3% daily loss flattens and pauses for the day.
- A 10% drawdown trips the kill switch.
- 5 consecutive losses pause entries for 4 h.
- More than 100 bps slippage is refused.
- Data older than 300 s trips the kill switch.
- Single-use 30 s risk approvals.

## Commands

**Start a run.** Push a change to the trigger file. One run lasts about 5.6 h; only one run at a time.

```bash
date -u +%FT%TZ > TRADEBOT/deploy/paper_run.trigger && git add TRADEBOT/deploy/paper_run.trigger && git commit -m "start paper run" && git push
```

**Status.** Open the Actions tab, workflow `paper-trading`, then the latest run:
https://github.com/mustafaghazi134-sudo/Mustafa.Ghazi/actions/workflows/paper-trading.yml

During a run, the checkpoint artifacts `paper-cp1-4m` … `paper-cp7-240m` hold:

- whether the engine is running;
- the kill and halt state;
- positions and intents;
- the last 30 decisions;
- orders, fills, trades and events;
- the log tail.

**Stop.** The kill switch flattens paper positions, then the engine stops within about 5 minutes:

```bash
touch TRADEBOT/deploy/PAPER_STOP && git add TRADEBOT/deploy/PAPER_STOP && git commit -m "stop paper run" && git push
```

Before the next start, delete `TRADEBOT/deploy/PAPER_STOP` and push. Cancelling the run in the Actions tab also
stops it immediately. Paper state is saved per cycle, and crash protection reconciles on the next start.

## Where records are stored

| Record | Location |
|---|---|
| Ledger (orders, fills, positions, intents, decisions, events, equity, trades) | `data_paper/ledger.sqlite` on the runner. Carried between runs by the Actions cache; downloadable in the `paper-final-<run id>` artifact (kept 90 days) |
| Log | `data_paper/tradebot.log` and `run_stdout.log` in the same artifact |
| Checkpoints | `data_paper/checkpoints/*.txt` in the `paper-cp*` and final artifacts |

## Does it keep running after the Claude session closes?

Yes, continuously. GitHub limits one job to 6 hours, so each run lasts about 5.6 h and then **starts the next run
itself** (`gh workflow run`, using only the workflow's own token). The ledger carries over through the Actions cache,
so the paper record accumulates across runs. Expect a gap of a few minutes between runs; positions and intents are
reconciled on each start.

The chain stops on its own, leaving a warning in the run, when:

- `TRADEBOT/deploy/PAPER_STOP` exists on the branch (your stop request);
- the engine was not running at the end of the run (crash: read `run_stdout.log` in the final artifact);
- the kill switch tripped during the run (for example the 10% drawdown limit). The next start would clear it, so the
  chain refuses to restart until you have looked.

To resume after any of these: delete `PAPER_STOP` if present, then push the trigger file (command above).

## Paper record and the live gate

`tradebot arm-live` requires **60 days** since the first paper event, the engine running for **at least 80%** of that
time (measured from its 5-minute equity snapshots), and **at least 5** closed paper trades. The old requirement of 30
trades in 14 days could not be met by a strategy that trades about 0.1 times a day; it would have taken 6-10 months.
The gate is operational: it shows the engine runs unattended and every order path works. It is **not** proof of an
edge; with a handful of trades nothing can be proved. Raising size beyond the small `[live]` caps still requires the
pre-registered review in `STRATEGY_AUDIT_GO_NOGO.md` section 9.

The gate reads the ledger on the machine where `arm-live` runs. The paper record built here lives in the Actions
cache and the `paper-final-*` artifacts, not on your PC. Before going live, either run the paper engine on the same
machine that will trade, or bring the runner's `ledger.sqlite` across and confirm `tradebot status` shows the record.
