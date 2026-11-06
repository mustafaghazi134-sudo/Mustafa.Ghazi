"""Fetch official venue/regulator/issuer pages as text so their claims can be checked against the primary source
(the build container cannot reach these hosts). Read-only; no accounts, no keys.
Writes research_lab/data_ext/official_pages/<name>.txt plus index.txt (URL, HTTP status, bytes, fetch time).
usage: python deploy/fetch_official_pages.py
"""
from __future__ import annotations

import html.parser
import os
import re
import time
import urllib.request

OUT = os.path.join(os.path.dirname(__file__), "..", "research_lab", "data_ext", "official_pages")
PAGES = {
    "hyperliquid_fees": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
    "hyperliquid_funding": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding",
    "hyperliquid_margin_tiers": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers",
    "hyperliquid_order_types": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types",
    "hyperliquid_api_wallets": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets",
    "hyperliquid_terms": "https://app.hyperliquid.xyz/terms",
    "hyperliquid_foundation_terms": "https://hyperliquid.xyz/terms",
    "jupiter_perps_fees": "https://docs.jup.ag/user-docs/trade/perps/fees",
    "jupiter_perps_faq": "https://docs.jup.ag/user-docs/trade/perps/faq",
    "jupiter_perps_liquidation": "https://docs.jup.ag/user-docs/trade/perps/liquidation",
    "jupiter_terms": "https://developers.jup.ag/docs/legal/terms-of-use",
    "jupiter_terms_site": "https://jup.ag/terms",
    "bybit_terms": "https://www.bybit.com/en/terms-service/information",
    "binance_terms": "https://www.binance.com/en/terms",
    "okx_terms": "https://www.okx.com/help/terms-of-service",
    "kraken_restricted": "https://support.kraken.com/articles/where-is-kraken-licensed-or-regulated",
    "coinbase_cbbtc": "https://www.coinbase.com/cbbtc",
    "coinbase_availability": "https://help.coinbase.com/en/coinbase/getting-started/crypto-education/where-is-coinbase-available",
    "wormhole_guardians": "https://wormhole.com/docs/protocol/infrastructure/guardians/",
    "jsc_home": "https://www.jsc.gov.jo/",
}


class Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"): self.skip += 1
        if tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div", "td"): self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip: self.skip -= 1

    def handle_data(self, d):
        if not self.skip: self.parts.append(d)


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research fetch; read-only)", "Accept": "text/html,text/markdown,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    index = []
    for name, url in PAGES.items():
        best = (0, "", url)
        for u in (url + ".md", url) if "gitbook" in url or "docs.jup.ag" in url else (url,):
            st, body = fetch(u)
            if st == 200 and len(body) > len(best[1]):
                best = (st, body, u)
            if st != 200 and not best[1]:
                best = (st, body, u)
            time.sleep(1)
        st, body, used = best
        if "<html" in body[:2000].lower():
            p = Text(); p.feed(body); body = "".join(p.parts)
        body = re.sub(r"\n\s*\n+", "\n\n", body)
        with open(os.path.join(OUT, f"{name}.txt"), "w") as f:
            f.write(f"SOURCE {used}\nFETCHED {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} HTTP {st}\n\n{body}")
        index.append(f"{name}\tHTTP {st}\t{len(body)} chars\t{used}")
        print(index[-1], flush=True)
    with open(os.path.join(OUT, "index.txt"), "w") as f:
        f.write("\n".join(index) + "\n")
