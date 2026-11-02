# Security and fit review: Jakeschincariol/day-trader-skill

Date 2026-11-02. Reviewed commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` (2026-10-20, plugin version 2.0.0, MIT).
Source: https://github.com/Jakeschincariol/day-trader-skill. The repository was cloned read-only into a scratch folder;
nothing from it ran before the static review below was complete.

## Outcome

| Decision | Skills |
|---|---|
| **Installed** (project-local, pinned, hash-checked) | `trader-timemachine` |
| **Rejected: order path / credentials** | `trader-broker` |
| **Rejected: stock-only and duplicates TRADEBOT** | `trader-risk`, `trader-budget`, `trader-scanner`, `trader-entry`, `trader-exit`, `trader-alerts` |
| **Rejected: not applicable to SOL, BTC or ETH** | `trader-news`, `trader-analyst`, `trader-pelosi` |

Installed at `TRADEBOT/.claude/skills/trader-timemachine/` (the README's documented project-local route: copy the
folder into the repo's `.claude/skills/`). The plugin-marketplace route was not used because it tracks the upstream
default branch; the folder copy is pinned to the reviewed commit by `.claude/skills.lock.json`, and
`tests/test_vendored_skills.py` fails if a single byte changes.

## 1. Security review (all 11 skills)

Static checks over every `.py` file and every `SKILL.md`, then the repository's own test suite.

| Check | Result |
|---|---|
| Third-party Python dependencies | **None.** Standard library only (`urllib`, `json`, `csv`, `zipfile`, `xml.etree`, `gzip`, `hashlib`, `random`, `statistics`...). Nothing to `pip install`. |
| Network | Plain HTTPS **GET** with a fixed User-Agent, no request bodies, to fixed public hosts: Yahoo Finance chart/RSS, Stooq, Nasdaq Trader symbol lists, SEC EDGAR, House Clerk. No credentials sent anywhere. |
| Process execution | One call: `trader-pelosi/ptr.py` runs a local `pdftotext` binary (found via `shutil.which`, argument list, no shell, 60 s timeout) on a downloaded disclosure PDF. Not installed. |
| Dynamic code (`eval`, `exec`, `pickle`, `marshal`, `ctypes`, raw sockets) | None in any skill. `importlib` appears only in the test loader. |
| Environment reads | Only `SEC_USER_AGENT` (news, analyst). No API keys, wallet keys or brokerage secrets are read by any script. |
| File writes | Only under `trader/` in the working directory, atomically (temp file + rename), and only with `--write`. `/trader/` added to `.gitignore`. |
| Skill instructions (`SKILL.md`) | No `allowed-tools` grants, no hooks, no settings changes, no instructions to fetch remote prompts. Every skill repeats that only `trader-broker` can lead to an order, after a risk PASS, a typed "yes" and Claude's own Allow-once card. |
| Order capability | Only `trader-broker`. It builds an order ticket; the order itself is sent by an external brokerage MCP server (Alpaca via `uvx alpaca-mcp-server`, unpinned, keys in env vars; or the Interactive Brokers connector). **This is the one material risk in the pack and the reason it was rejected.** |
| Tests | `python -m unittest discover -s tests`: **315 tests, all pass, offline**, leaving only `__pycache__` behind. |

Verdict: the code is safe to execute as research tooling. The risk is not malicious code; it is the broker path, which
would put brokerage credentials and an order tool into Claude's reach. That is excluded by design here.

## 2. Fit for a Solana spot bot: what is stock-market-specific

Every skill is built around the US equity session, and several refuse crypto outright. Verified by running the
unmodified tools on real SOL data (Binance 1m klines, converted to Yahoo format), not by reading alone:

| Skill | Stock-market assumptions that break for SOL / BTC / ETH on Jupiter | Duplicates TRADEBOT? |
|---|---|---|
| trader-scanner | Universe = Nasdaq Trader US stocks/ADRs; ticker regex `^[A-Z]{1,5}$` rejects `SOL-USD`; NYSE session and holiday calendar; 9:30 opening range | yes: `tradebot/scanner` |
| trader-entry | 9:30 NY opening-range breakout on Yahoo stock bars; NYSE calendar; long only | yes: strategies and scanner signals |
| trader-exit | Cent-rounded stops, "flat by 15:55" NY, opening-range midpoint stop | yes: stops/targets in both engines |
| trader-timemachine | Replays only 9:30-16:00 NY, needs 78-bar "full sessions", skips weekends and NYSE holidays; **no trading costs in its average R**. Runs on crypto bars if given an offline file. | **no**: TRADEBOT has no Monte Carlo bootstrap of a single trade plan |
| trader-budget | Whole shares, cent rounding, 75 % default position cap, Alpaca `place_stock_order` JSON, no fees. On a SOL plan it sized 7 "shares" = $733 of a $1,000 account. | yes: `RiskEngine` sizing (10 % cap) |
| trader-risk | **Blocks crypto by rule**: `SOL-USD does not look like a US stock or ETF ticker; this pack trades stocks and ETFs only`. PDT rule, NYSE entry window, margin/short checks. | yes: independent `RiskEngine` |
| trader-broker | Alpaca / IBKR stock orders | n/a: rejected |
| trader-alerts | NYSE opening bell and early closes, Yahoo/Stooq stock quotes | yes: engines manage exits and the kill switch |
| trader-news | Yahoo per-ticker headlines plus SEC 8-K filings, tagged for equities | no, but not useful for the three traded assets |
| trader-analyst | SEC XBRL company financials | not applicable |
| trader-pelosi | US House stock-trade disclosures | not applicable |

None of them can quote Jupiter, read a Solana mint, price cbBTC or WETH, or account for swap fees, price impact or
network fees.

## 3. The one useful skill: Time Machine

What it adds that TRADEBOT lacks: for one concrete trade plan (entry, stop, target), it rebuilds the rest of the day
10,000 times by drawing each 5-minute bar from the asset's own bars at the same time of day over ~60 sessions, and
separately re-runs the same rule on every past session. It reports target-first, stop-first and neither, the average R,
and a plain verdict. Deterministic with `--seed`.

Measured on SOL (unmodified skill, real Binance data, 20 October 2026 opening-range breakout, entry 104.76, stop
104.28, target 105.24):

| Output | Value |
|---|---|
| Replays: target first / stop first / neither | 53.8 % / 46.2 % / 0.0 % |
| Average R before costs | +0.075 |
| Same rule on 57 past sessions | fired 48 times: 18 target, 30 stop, average **−0.25 R** |
| Skill's verdict | "Close to a coin flip" |
| Jupiter round trip of 30 / 40 / 52 bps expressed in R | **0.65 / 0.87 / 1.13 R** |

The skill's own numbers say this setup has no edge before costs, and the cost conversion shows it is strongly
negative after them. That is consistent with `REALTIME_SCANNER_EVAL.md`: intraday crypto setups at retail taker costs
lose. The Time Machine is useful precisely because it makes that visible for a single proposed trade in seconds.

How to use it in TRADEBOT research (never in the execution loop):

1. Export verified bars: `python -m research_lab.yahoo_chart_export --symbol SOL-USD --days 60 --end <date> --out trader/SOL.json.gz <1m CSVs>`.
2. Run it offline: `python .claude/skills/trader-timemachine/timemachine.py run --offline trader/SOL.json.gz --symbol SOL-USD --date ... --entry ... --stop ... --target ... --entry-time "..." --now "..."`, or ask Claude `/trader-timemachine` in this project.
3. Subtract costs yourself: `cost_in_R = round_trip_bps / 1e4 × entry / (entry − stop)`, using the round trip from `tradebot verify-assets`.
4. Read the result as a reality check on a single setup. It samples only 9:30–16:00 New York weekdays, so it says
   nothing about the other 17.5 hours or weekends, and 60 days is one regime.

## 4. Isolation and permissions

- Location: `.claude/skills/` is outside the `tradebot` package (setuptools includes `tradebot*` only), so the engines
  cannot import it; a test asserts no `tradebot` module references it.
- The skill runs only when a person or Claude invokes it, writes only to `trader/`, and has no credentials.
- `TRADEBOT/.claude/settings.json` (project settings, deny-only): denies the `alpaca` and `ibkr` MCP servers and their
  order tools, and denies shell commands that invoke `trader-broker` or `ticket.py`. It contains no allow rules, no
  default mode, no MCP servers and no hooks.
- No brokerage account, wallet, Jupiter key or Anthropic key is connected to, or readable by, the skill.
- PAPER mode remains the default for both TRADEBOT engines; nothing here changes the live gate.

## 5. Upgrade procedure

Re-clone upstream, diff against `3d0f4ef`, repeat sections 1–2 for the changed files, copy the reviewed files
byte-for-byte, update commit and hashes in `.claude/skills.lock.json`, run `python -m pytest`.
