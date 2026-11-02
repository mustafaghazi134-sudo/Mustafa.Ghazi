"""Read-only preflight for the live adapter. Signs nothing, submits nothing, never touches a private key.

Checks: build switch, mode and live gate, [live] limits sanity, wallet public key, legacy key variables in the
trading environment, Jupiter key presence (never its value), Jupiter /order quotes for every allowed symbol
(router, transaction version, fees, impact), Solana RPC health and block height, wallet balances and SOL reserve,
the signer's health (wallet, activation, halt) without any signing request, live halt file, unresolved intents.
"""
from __future__ import annotations

import os

from ..config import Settings, jupiter_endpoints
from ..models import Side
from ..portfolio.ledger import Ledger
from . import tx_verify as V
from .live_gate import LiveGate
from .submission import submission_enabled

Row = tuple[str, str, str]


def live_preflight(settings: Settings, adapter=None, signer=None) -> list[Row]:
    rows: list[Row] = []
    cfg = settings.live
    rows.append(("build_submission_switch", "OK", "disabled (MAINNET_SUBMISSION_ENABLED = False): no transaction can be sent"
                 if not submission_enabled() else "ENABLED in this build"))
    st = LiveGate(settings).status()
    rows.append(("live_gate", "OK" if not st["armed"] else "WARN", f"mode={settings.mode} armed={st['armed']} env_ok={st['env_ok']} "
                 f"armed_file={st['armed_file']} limits_match={st['limits_match']}"))
    for name in ("SOLANA_PRIVATE_KEY", "SOLANA_KEYPAIR_FILE"):
        rows.append((f"env {name}", "WARN" if os.environ.get(name) else "OK",
                     "SET in the trading environment: remove it; only the signer service may hold the key" if os.environ.get(name) else "not set"))
    rows.append(("live_limits", "OK" if cfg.max_order_usd <= cfg.max_wallet_exposure_usd else "FAIL",
                 f"order ${cfg.max_order_usd:g}, exposure ${cfg.max_wallet_exposure_usd:g}, daily loss ${cfg.max_daily_loss_usd:g}, "
                 f"daily notional ${cfg.max_daily_notional_usd:g}, {cfg.max_orders_per_day} orders/day, slippage {cfg.max_slippage_bps} bps"))
    bad_routers = sorted(set(cfg.allowed_routers) - V.VERIFIABLE_ROUTERS)
    rows.append(("routers", "WARN" if bad_routers else "OK", f"allowed {sorted(set(cfg.allowed_routers) & V.VERIFIABLE_ROUTERS)}"
                 + (f"; ignored (unverifiable programs): {bad_routers}" if bad_routers else "")))
    rows.append(("wallet_pubkey", "OK" if cfg.wallet_pubkey else "FAIL", cfg.wallet_pubkey or "[live] wallet_pubkey not set"))
    jup = jupiter_endpoints(settings.data, settings.execution)
    rows.append(("jupiter_api_key", "OK" if jup.has_key else "FAIL", "present (value not displayed)" if jup.has_key else "missing"))
    halt = settings.live_halt_file
    rows.append(("live_halt", "WARN" if halt.exists() else "OK", "LIVE_HALT present" if halt.exists() else "clear"))
    try:
        pend = Ledger(settings.db_path, mode="live").pending_intents()
        rows.append(("unresolved_live_intents", "WARN" if pend else "OK", f"{len(pend)} pending"))
    except Exception as e:  # noqa: BLE001
        rows.append(("unresolved_live_intents", "WARN", f"ledger unavailable: {e}"))
    if adapter is not None:
        try:
            bh = adapter.rpc("getBlockHeight", [{"commitment": "confirmed"}])
            rows.append(("rpc", "OK", f"block height {bh} via {settings.execution.rpc_url}"))
        except Exception as e:  # noqa: BLE001
            rows.append(("rpc", "FAIL", f"{type(e).__name__}: {e}"))
        try:
            snap = adapter.wallet_snapshot()
            ok = snap.lamports >= cfg.min_sol_reserve_lamports
            rows.append(("wallet_balances", "OK" if ok else "WARN",
                         f"SOL {snap.lamports / 1e9:.4f} (reserve {cfg.min_sol_reserve_lamports / 1e9:g}); tokens "
                         + ", ".join(f"{k} {v:.6g}" for k, v in sorted(snap.holdings.items())) + f"; exposure ${snap.exposure_usd:.2f}"))
        except Exception as e:  # noqa: BLE001
            rows.append(("wallet_balances", "FAIL", f"{type(e).__name__}: {e}"))
        size = min(5.0, cfg.max_order_usd)
        for sym in cfg.allowed_symbols:
            try:
                q = adapter.quote(sym, Side.BUY, size)
                rows.append((f"quote USDC->{sym}", "OK" if q.expected_slippage_bps <= cfg.max_total_cost_bps else "WARN",
                             f"${size:g}: router={q.raw.get('router')} cost vs reference {q.expected_slippage_bps:.1f} bps, fee ${q.fee_usd:.4f}"))
            except Exception as e:  # noqa: BLE001
                rows.append((f"quote USDC->{sym}", "FAIL", f"{type(e).__name__}: {e}"))
    if signer is not None:
        try:
            h = signer.health()
            good = h.get("wallet") == cfg.wallet_pubkey and not h.get("halted")
            rows.append(("signer", "OK" if good else "WARN", f"wallet match={h.get('wallet') == cfg.wallet_pubkey} active={h.get('active')} "
                         f"({h.get('activation')}) halted={h.get('halted')} build_switch={h.get('submission_enabled_in_build')} "
                         f"signed today={h.get('signatures_today')}/{h.get('max_signatures_per_day')}"))
        except Exception as e:  # noqa: BLE001
            rows.append(("signer", "WARN", f"not reachable: {type(e).__name__}: {e}"))
    else:
        rows.append(("signer", "WARN", "not configured ([live] signer_secret_file / TRADEBOT_SIGNER_SECRET)"))
    return rows
