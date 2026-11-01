# Data provenance for research_lab results

All candles are Binance spot 1h klines in the official `data.binance.vision` monthly-dump layout
(`<SYMBOL>USDT-1h-<YYYY-MM>.csv`), mirrored in public GitHub repositories. Six independent mirrors were
loaded; where months overlap, closes were compared hour by hour (`research_lab/data.py`, `LoadReport`).

| Mirror (GitHub) | Path inside repo | Symbols | Months | Agreement with others |
|---|---|---|---|---|
| changhaowuwu/LSTM-live-cripto-forecast | `data/` | BTC, ETH (+AVAX, DOT) | 2019-09 .. 2024-04 | 0.0000% close deviation |
| nawr1/RL_trading | `data/raw_data/` | BTC | 2019-01 .. 2026-04 | 0.0000% |
| el-frontend/frontbot | `data/data/1h/` | BTC | 2023-01 .. 2026-09 | 0.0000% |
| 7huahua/RedditDataset | `data/prices/binance_1h_klines/` | BTC, ETH, SOL (+BNB, DOGE, PEPE) | 2024-03 .. 2025-04 | 0.0000% |
| Anas436/Predicting-Cryptocurrency-AI | `src/data/` | BTC, ETH | 2019-09 .. 2023-05 | 0.0000% |
| spurs7216/Quant_Research | `data/` | SOL (+BTC, ETH and 9 alts) | 2022-01 .. 2023-11 | mean 0.04%, max 0.86% vs others: likely a perpetual-futures feed |

Resulting series used (hourly bars, gaps > 72h split into blocks):

| Series | Span | Bars | Note |
|---|---|---|---|
| BTCUSDT | 2019-01-01 .. 2026-09-30 | 67,861 | 20 small intra-day gaps (exchange maintenance) |
| ETHUSDT | 2019-09-01 .. 2025-04-30 | 49,621 | |
| SOLUSDT block 1 | 2022-03-01 .. 2023-11-30 | 15,312 | only source is the 0.04%-deviating mirror |
| SOLUSDT block 2 | 2024-03-01 .. 2025-04-30 | 10,224 | |

Not obtainable offline: SOL 2020-08 .. 2021-12, SOL 2023-12 .. 2024-02, SOL after 2025-04, ETH after 2025-04.

**SOL is UNVALIDATED.** Both SOL blocks produced too few out-of-sample trades to judge any strategy
(8 trades for the selected strategy on block 1, fewer on block 2), and a random-entry control was also
profitable on those blocks. SOL stays in the universe only because the same long-only trend rules apply
to it; its parameters have not been shown to work on SOL specifically.

## Reproduce

```bash
cd TRADEBOT
pip install -e ".[research]"
bash research_lab/fetch_data.sh /path/to/data_raw        # sparse-clones the six mirrors (~110 MB)
export PYTHONPATH=$PWD
python -m research_lab.run_all --data-dirs /path/to/data_raw/* --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframe 4h --out results_4h
python -m research_lab.run_all --data-dirs /path/to/data_raw/* --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframe 1D --train-months 18 --test-months 6 --out results_1d
python -m research_lab.run_all --data-dirs /path/to/data_raw/* --symbols BTCUSDT,ETHUSDT,SOLUSDT --out results_1h
python -m research_lab.report results_4h/results.json --out RESULTS_4h.md
```

Code version: the results committed under `research_lab/results/` were produced at commit `16b0811`
(1h, 4h, 1D batches) with costs 10 bps fee + 15 bps slippage per side, walk-forward 12m/3m (18m/6m on 1D),
warm-up 800/300/120 bars. The runner is deterministic; re-running on the same files reproduces the JSON
(timestamps aside). The whole-bot replay (`RESULTS.md`, "Bot-level replay") was run at commit `d97fff8`
with `tradebot run --cycles 20000` on the exported CSVs (`research_lab/export_csv.py` reproduces them)
and the config shown in that section.
