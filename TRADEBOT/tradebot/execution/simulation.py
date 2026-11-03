"""Measure what a transaction would do to the wallet, by simulation, before anything is signed.

`simulateTransaction` (sigVerify=false, so no signature is needed) returns the post-state of the accounts we name.
Comparing it with the current state gives the wallet's real balance changes: native SOL (wallet lamports plus the
wallet's wSOL token account) and every allow-listed mint's associated token account. This does not depend on
decoding the swap instruction, so it also protects against a route that does something other than it claims.

Pure logic plus a caller-supplied `rpc(method, params) -> result` function (the adapter's and the signer's own).
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from typing import Callable

from solders.address_lookup_table_account import AddressLookupTable

from .tx_verify import TOKEN_PROGRAM, WSOL_MINT, ata_address

Rpc = Callable[[str, list], dict]


@dataclass
class SimResult:
    err: object = None
    units: int | None = None
    native_delta_lamports: int = 0                    # wallet lamports + wSOL token amount, after minus before
    token_deltas: dict[str, int] = field(default_factory=dict)   # mint -> raw delta of the wallet's ATA
    logs: list[str] = field(default_factory=list)

    def delta(self, mint: str) -> int:
        return self.native_delta_lamports if mint == WSOL_MINT else self.token_deltas.get(mint, 0)


def _token_amount(acc: dict | None) -> int:
    if not acc:
        return 0
    data = acc.get("data")
    raw = base64.b64decode(data[0]) if isinstance(data, list) and data else b""
    if len(raw) < 72:
        return 0
    return struct.unpack_from("<Q", raw, 64)[0]


def _lamports(acc: dict | None) -> int:
    return int(acc.get("lamports", 0)) if acc else 0


def watch_addresses(wallet: str, mints: list[str], token_programs: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """[(label, address)]: the wallet itself, then each mint's ATA (wSOL included)."""
    tp = token_programs or {}
    out = [("wallet", wallet)]
    for m in dict.fromkeys([*mints, WSOL_MINT]):
        out.append((m, ata_address(wallet, m, tp.get(m, TOKEN_PROGRAM))))
    return out


def simulate_deltas(rpc: Rpc, tx_b64: str, wallet: str, mints: list[str], token_programs: dict[str, str] | None = None) -> SimResult:
    watch = watch_addresses(wallet, mints, token_programs)
    addrs = [a for _, a in watch]
    pre = rpc("getMultipleAccounts", [addrs, {"encoding": "base64", "commitment": "confirmed"}])["value"]
    sim = rpc("simulateTransaction", [tx_b64, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": False,
                                               "commitment": "confirmed", "accounts": {"encoding": "base64", "addresses": addrs}}])["value"]
    post = sim.get("accounts") or [None] * len(addrs)
    res = SimResult(err=sim.get("err"), units=sim.get("unitsConsumed"), logs=list(sim.get("logs") or [])[-20:])
    for i, (label, _) in enumerate(watch):
        if label == "wallet":
            res.native_delta_lamports += _lamports(post[i]) - _lamports(pre[i])
        elif label == WSOL_MINT:
            res.native_delta_lamports += _token_amount(post[i]) - _token_amount(pre[i])
        else:
            res.token_deltas[label] = _token_amount(post[i]) - _token_amount(pre[i])
    return res


def check_deltas(sim: SimResult, input_mint: str, output_mint: str, max_in_raw: int, min_out_raw: int,
                 max_sol_cost_lamports: int) -> list[str]:
    """The wallet may lose at most `max_in_raw` of the input (plus SOL fees), must gain at least `min_out_raw` of the
    output (net of fees when the output is SOL), and nothing else it holds may decrease."""
    p = []
    if sim.err is not None:
        return [f"simulation failed: {sim.err}"]
    spent_in = -sim.delta(input_mint)
    got_out = sim.delta(output_mint)
    sol_cost = 0
    if input_mint == WSOL_MINT:
        if spent_in > max_in_raw + max_sol_cost_lamports:
            p.append(f"simulation: wallet spends {spent_in} lamports for an order of {max_in_raw} (+fee cap {max_sol_cost_lamports})")
    else:
        if spent_in > max_in_raw:
            p.append(f"simulation: wallet spends {spent_in} raw {input_mint}, more than the order's {max_in_raw}")
        elif spent_in < max_in_raw * 0.99:
            # the input must leave the watched (associated) token account; anything else means the simulation is not
            # observing the funds the transaction actually moves, so it proves nothing
            p.append(f"simulation: the watched {input_mint} account is debited {spent_in}, not the order's {max_in_raw} "
                     "(input held outside the wallet's associated token account?)")
        if output_mint != WSOL_MINT:
            sol_cost = -sim.native_delta_lamports
            if sol_cost > max_sol_cost_lamports:
                p.append(f"simulation: wallet SOL falls by {sol_cost} lamports (fees cap {max_sol_cost_lamports})")
    if output_mint == WSOL_MINT:
        if got_out + max_sol_cost_lamports < min_out_raw:
            p.append(f"simulation: wallet gains {got_out} lamports net, below the minimum {min_out_raw} less fees")
    elif got_out < min_out_raw:
        p.append(f"simulation: wallet gains {got_out} raw {output_mint}, below the minimum {min_out_raw}")
    for m, d in sim.token_deltas.items():
        if m not in (input_mint, output_mint) and d < 0:
            p.append(f"simulation: unrelated balance {m} decreases by {-d}")
    return p


def alt_resolver_from_rpc(rpc: Rpc) -> Callable[[str], list[str]]:
    cache: dict[str, list[str]] = {}

    def resolve(table: str) -> list[str]:
        if table not in cache:
            res = rpc("getAccountInfo", [table, {"encoding": "base64", "commitment": "confirmed"}])
            val = res.get("value") if isinstance(res, dict) else None
            if not val:
                raise ValueError("lookup table account not found")
            data = base64.b64decode(val["data"][0])
            cache[table] = [str(a) for a in AddressLookupTable.deserialize(data).addresses]
        return cache[table]
    return resolve
