"""Bounded, read-only fetch of PUBLIC research data for the long/short feasibility study (run on a GitHub runner;
the build container cannot reach market hosts). No keys, no accounts, nothing signed.

Writes CSVs to research_lab/data_ext/:
  funding_binance_<SYM>.csv      Binance USD-M perpetual funding history (data.binance.vision monthly dumps)
  funding_hyperliquid_<COIN>.csv Hyperliquid hourly funding history (public info API)
  wrapped_llama_<NAME>.csv       DefiLlama daily prices: Solana cbBTC and Wormhole WETH vs coingecko BTC/ETH
  wrapped_gt_<NAME>.csv          GeckoTerminal daily OHLCV + pool liquidity of the deepest Solana pools for each wrapper
  fetch_log.txt                  what succeeded or failed
usage: python deploy/fetch_research_data.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

OUT = os.path.join(os.path.dirname(__file__), "..", "research_lab", "data_ext")
UA = {"User-Agent": "tradebot-research-fetch/1.0", "Accept": "application/json"}
LOG: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG.append(msg)


def get(url: str, data: bytes | None = None, tries: int = 4) -> bytes | None:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={**UA, **({"Content-Type": "application/json"} if data else {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(10 * (i + 1)); continue
            log(f"HTTP {e.code} {url[:120]}")
            time.sleep(2 * (i + 1))
        except Exception as e:  # noqa: BLE001
            log(f"ERR {type(e).__name__} {url[:120]}: {str(e)[:100]}")
            time.sleep(2 * (i + 1))
    return None


def write(name: str, header: list[str], rows: list[list]) -> None:
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)
    log(f"wrote {name}: {len(rows)} rows")


def binance_funding() -> None:
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        rows = []
        for y in range(2019, 2027):
            for m in range(1, 13):
                if (y, m) > (2026, 10):
                    break
                b = get(f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{y}-{m:02d}.zip")
                if not b:
                    continue
                with zipfile.ZipFile(io.BytesIO(b)) as z:
                    txt = z.read(z.namelist()[0]).decode()
                for line in txt.splitlines():
                    p = line.split(",")
                    if not p[0].strip().isdigit():
                        continue
                    rows.append([int(p[0]), float(p[-1])])          # calc_time ms, funding rate per interval
        rows.sort()
        write(f"funding_binance_{sym}.csv", ["time_ms", "rate"], rows)


def hyperliquid_funding() -> None:
    for coin in ("BTC", "ETH", "SOL"):
        rows, start = [], 1_682_899_200_000          # 2023-05-01
        end = int(time.time() * 1000)
        while start < end:
            b = get("https://api.hyperliquid.xyz/info", data=json.dumps({"type": "fundingHistory", "coin": coin, "startTime": start}).encode())
            if not b:
                log(f"hyperliquid {coin}: stopped at {start}"); break
            d = json.loads(b)
            if not d:
                break
            rows += [[int(x["time"]), float(x["fundingRate"]), float(x.get("premium") or 0)] for x in d]
            nxt = int(d[-1]["time"]) + 1
            if nxt <= start:
                break
            start = nxt
            time.sleep(0.4)
        write(f"funding_hyperliquid_{coin}.csv", ["time_ms", "rate_per_hour", "premium"], rows)


WRAPPERS = {"cbBTC": ("cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", "coingecko:bitcoin"),
            "WETH_wormhole": ("7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "coingecko:ethereum"),
            "SOL": ("So11111111111111111111111111111111111111112", "coingecko:solana")}


def llama_prices() -> None:
    start = 1_640_995_200   # 2022-01-01
    for name, (mint, ref) in WRAPPERS.items():
        out = {}
        for key in (f"solana:{mint}", ref):
            b = get(f"https://coins.llama.fi/chart/{key}?start={start}&span=2000&period=1d&searchWidth=600")
            if not b:
                log(f"llama {key}: no data"); continue
            pts = (json.loads(b).get("coins", {}).get(key) or {}).get("prices", [])
            for p in pts:
                day = int(p["timestamp"]) // 86400 * 86400
                out.setdefault(day, {})[key] = p["price"]
        rows = [[d, v.get(f"solana:{mint}"), v.get(ref)] for d, v in sorted(out.items())]
        write(f"wrapped_llama_{name}.csv", ["day_ts", "solana_token_usd", "reference_usd"], rows)


def geckoterminal() -> None:
    for name, (mint, _) in WRAPPERS.items():
        if name == "SOL":
            continue
        b = get(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools?page=1")
        if not b:
            log(f"gt pools {name}: none"); continue
        pools = json.loads(b).get("data", [])[:5]
        meta = []
        for p in pools:
            a = p["attributes"]
            meta.append({"pool": a["address"], "name": a.get("name"), "dex": p["relationships"]["dex"]["data"]["id"],
                         "reserve_usd": a.get("reserve_in_usd"), "vol24h_usd": (a.get("volume_usd") or {}).get("h24")})
        with open(os.path.join(OUT, f"wrapped_gt_{name}_pools.json"), "w") as f:
            json.dump(meta, f, indent=1)
        log(f"gt {name} top pools: " + "; ".join(f"{m['name']} {m['dex']} reserve ${float(m['reserve_usd'] or 0):,.0f} vol24h ${float(m['vol24h_usd'] or 0):,.0f}" for m in meta))
        rows = []
        for m in meta[:2]:
            time.sleep(2.5)
            b = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{m['pool']}/ohlcv/day?limit=1000&currency=usd&token={mint}")
            if not b:
                continue
            for o in json.loads(b)["data"]["attributes"]["ohlcv_list"]:
                rows.append([m["pool"], int(o[0]), *o[1:6]])
            time.sleep(2.5)
            b = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{m['pool']}/ohlcv/hour?limit=1000&currency=usd&token={mint}")
            if b:
                hr = [[m["pool"], int(o[0]), *o[1:6]] for o in json.loads(b)["data"]["attributes"]["ohlcv_list"]]
                write(f"wrapped_gt_{name}_{m['pool'][:8]}_1h.csv", ["pool", "ts", "open", "high", "low", "close", "volume_usd"], hr)
        write(f"wrapped_gt_{name}_1d.csv", ["pool", "ts", "open", "high", "low", "close", "volume_usd"], rows)


def binance_reference() -> None:
    for pair in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        rows = []
        for itv, lim in (("1d", 1000), ("1h", 1000)):
            b = get(f"https://data-api.binance.vision/api/v3/klines?symbol={pair}&interval={itv}&limit={lim}")
            if b:
                rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in json.loads(b)]
                write(f"binance_{pair}_{itv}.csv", ["open_ms", "open", "high", "low", "close"], rows)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for fn in (binance_funding, hyperliquid_funding, llama_prices, geckoterminal, binance_reference):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"{fn.__name__} FAILED {type(e).__name__}: {str(e)[:200]}")
    with open(os.path.join(OUT, "fetch_log.txt"), "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n" + "\n".join(LOG) + "\n")
