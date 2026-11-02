"""Pre-signing verification of a Jupiter Swap v2 `/order` response and the transaction it carries.

Pure functions, no network: the caller supplies the current block height, the wall clock and (for v0 transactions
with address lookup tables) a resolver that returns the addresses stored in each table. Both the trading process
(`JupiterExecutionAdapter`) and the separate signer service (`tradebot_signer`) run these checks; the signer runs
them independently on what it is actually asked to sign.

Reference: docs.jup.ag (jup-ag/docs, swap/order-and-execute.mdx, swap/advanced/transaction-versions.mdx,
openapi-spec/swap/v2/swap.yaml as of 2026-10-28):
  * `/order` returns a v0 transaction by default (`maxSupportedTransactionVersion` defaults to "0"); v1 is opt-in.
    This module accepts **v0 only** (the signing library here, solders 0.29, cannot parse v1, and v1 moves the
    compute budget onto the message config, which these checks do not cover).
  * `transaction` is "" with `errorCode` when the router priced but could not build; `requestId` goes to `/execute`.
  * aggregator routes expire at `lastValidBlockHeight`; JupiterZ (RFQ) quotes at `expireAt`; RFQ transactions carry
    the market maker as a second required signer (taker signs partially).
  * `priceImpact` is in percentage points (`priceImpactPct` is deprecated); `feeBps` is the total fee rate and
    `feeMint` the token it is taken in; `otherAmountThreshold` is the minimum output after slippage.

Every check fails closed: anything unrecognised is a problem, and any problem blocks signing.
"""
from __future__ import annotations

import base64
import binascii
import math
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA1knL"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
JUPITER_V6_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"        # Metis routes (docs: swap program IDL)
JUPITERZ_ORDER_ENGINE = "61DFfeTKM7trxYcPQCM78bJ794ddZprZpAwAnLiwTpYH"      # JupiterZ RFQ settlement (docs: rfq settlement)
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SWAP_PROGRAMS = frozenset({JUPITER_V6_PROGRAM, JUPITERZ_ORDER_ENGINE})
DEFAULT_ALLOWED_PROGRAMS = frozenset({SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ATA_PROGRAM,
                                      COMPUTE_BUDGET_PROGRAM, *SWAP_PROGRAMS})
# Routers whose top-level programs are known and allow-listed above. DFlow and OKX build transactions with their own
# programs, which this verifier cannot vouch for, so the adapter excludes them via `excludeRouters`.
VERIFIABLE_ROUTERS = frozenset({"metis", "jupiterz"})
ROUTER_PROGRAMS = {"metis": JUPITER_V6_PROGRAM, "jupiterz": JUPITERZ_ORDER_ENGINE}

SIGNATURE_FEE_LAMPORTS = 5_000
ATA_RENT_LAMPORTS = 2_039_280            # rent-exempt minimum of a 165-byte token account
DEFAULT_CU_PER_INSTRUCTION = 200_000
MAX_CU = 1_400_000


class VerificationError(ValueError):
    """Raised with the full list of problems; nothing may be signed."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


AltResolver = Callable[[str], list[str]]     # lookup-table address -> its addresses (in order)


@dataclass(frozen=True)
class TxPolicy:
    allowed_programs: frozenset[str] = DEFAULT_ALLOWED_PROGRAMS
    allowed_mints: frozenset[str] = frozenset({USDC_MINT, WSOL_MINT})
    max_priority_fee_lamports: int = 500_000
    max_external_sol_outflow_lamports: int = 500_000     # System transfers from the wallet to accounts it does not own (tips)
    max_rent_lamports: int = 2 * ATA_RENT_LAMPORTS
    max_signers: int = 2


@dataclass
class OrderExpectation:
    """What the trading process asked for. All amounts are raw integer units."""
    wallet: str
    input_mint: str
    output_mint: str
    in_amount_raw: int
    min_out_raw: int                     # our own floor (reference price, size and max total cost), not Jupiter's
    received_ts: float                   # when the /order response arrived


@dataclass(frozen=True)
class OrderPolicy:
    allowed_routers: frozenset[str] = VERIFIABLE_ROUTERS
    max_fee_bps: float = 25.0
    max_price_impact_bps: float = 30.0
    max_slippage_bps: int = 50
    max_priority_fee_lamports: int = 500_000
    max_rent_lamports: int = 2 * ATA_RENT_LAMPORTS
    min_blocks_remaining: int = 30
    min_rfq_seconds_remaining: float = 5.0
    max_quote_age_sec: float = 8.0
    allow_gasless: bool = True


@dataclass
class TxReport:
    version: str = ""
    fee_payer: str = ""
    signers: list[str] = field(default_factory=list)
    wallet_signer_index: int | None = None
    programs: list[str] = field(default_factory=list)
    recent_blockhash: str = ""
    compute_unit_limit: int | None = None
    compute_unit_price_micro: int = 0
    priority_fee_lamports: int = 0
    signature_fee_lamports: int = 0
    external_sol_outflow_lamports: int = 0
    wrap_lamports: int = 0
    rent_lamports: int = 0
    ata_creates: list[dict] = field(default_factory=list)
    swap_instructions: int = 0
    lookup_tables: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def wallet_sol_cost_lamports(self) -> int:
        """Upper bound of SOL the wallet spends on fees, tips and rent (excludes SOL wrapped for the swap itself)."""
        return self.priority_fee_lamports + self.signature_fee_lamports + self.external_sol_outflow_lamports + self.rent_lamports

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["wallet_sol_cost_lamports"] = self.wallet_sol_cost_lamports
        return d


# ---------------------------------------------------------------------------------------------------- helpers
def ata_address(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    pda, _ = Pubkey.find_program_address([bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)),
                                          bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA_PROGRAM))
    return str(pda)


def decode_transaction(tx_b64: str) -> VersionedTransaction:
    if not isinstance(tx_b64, str) or not tx_b64:
        raise VerificationError(["no transaction to verify"])
    try:
        raw = base64.b64decode(tx_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise VerificationError([f"transaction is not valid base64: {e}"]) from e
    if len(raw) > 1232:
        raise VerificationError([f"transaction is {len(raw)} bytes; v0 limit is 1232 (v1 is not accepted)"])
    try:
        return VersionedTransaction.from_bytes(raw)
    except Exception as e:  # noqa: BLE001 - solders raises ValueError for v1 / malformed
        raise VerificationError([f"transaction does not decode as a v0/legacy versioned transaction: {e}"]) from e


def resolve_account_keys(msg: MessageV0, alt_resolver: AltResolver | None) -> tuple[list[str], list[str]]:
    """(all account keys in instruction-index order, problems). Order: static, then every table's writable
    addresses, then every table's read-only addresses (Solana's v0 loading order)."""
    static = [str(k) for k in msg.account_keys]
    lookups = list(msg.address_table_lookups)
    if not lookups:
        return static, []
    if alt_resolver is None:
        return static, ["transaction uses address lookup tables but no resolver is available"]
    writable, readonly, problems = [], [], []
    for lk in lookups:
        try:
            table = alt_resolver(str(lk.account_key))
        except Exception as e:  # noqa: BLE001
            problems.append(f"lookup table {lk.account_key} could not be resolved: {e}")
            continue
        for idxs, out in ((lk.writable_indexes, writable), (lk.readonly_indexes, readonly)):
            for i in bytes(idxs):
                if i >= len(table):
                    problems.append(f"lookup table {lk.account_key} has no index {i}")
                else:
                    out.append(table[i])
    return static + writable + readonly, problems


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _u64(data: bytes, off: int) -> int:
    return struct.unpack_from("<Q", data, off)[0]


# ---------------------------------------------------------------------------------------------------- transaction
def inspect_transaction(tx: VersionedTransaction | str, wallet: str, policy: TxPolicy,
                        alt_resolver: AltResolver | None = None) -> TxReport:
    """Static analysis of every top-level instruction. Problems are collected, never raised."""
    if isinstance(tx, str):
        tx = decode_transaction(tx)
    rep = TxReport()
    msg = tx.message
    if not isinstance(msg, MessageV0):
        rep.version = "legacy"
        rep.problems.append("legacy transaction: only v0 is accepted")
        return rep
    rep.version = "v0"
    try:
        if tx.uses_durable_nonce():
            rep.problems.append("durable-nonce transaction (never expires): refused")
    except Exception:  # noqa: BLE001 - older solders: fall through to the System-instruction check below
        pass
    rep.recent_blockhash = str(msg.recent_blockhash)
    rep.lookup_tables = [str(lk.account_key) for lk in msg.address_table_lookups]
    static = [str(k) for k in msg.account_keys]
    keys, alt_problems = resolve_account_keys(msg, alt_resolver)
    rep.problems += alt_problems
    n_sig = msg.header.num_required_signatures
    rep.signers = static[:n_sig]
    rep.fee_payer = static[0] if static else ""
    if wallet in rep.signers:
        rep.wallet_signer_index = rep.signers.index(wallet)
    else:
        rep.problems.append("wallet is not a required signer of this transaction")
    if n_sig > policy.max_signers:
        rep.problems.append(f"{n_sig} required signers (max {policy.max_signers})")
    if len(tx.signatures) != n_sig:
        rep.problems.append("signature count does not match the message header")
    if rep.fee_payer == wallet:
        rep.signature_fee_lamports = SIGNATURE_FEE_LAMPORTS * n_sig
    wsol_ata = ata_address(wallet, WSOL_MINT)
    cu_limit, n_non_cb = None, 0

    def acct(ix, k: int) -> str | None:
        idx = bytes(ix.accounts)
        if k >= len(idx) or idx[k] >= len(keys):
            return None
        return keys[idx[k]]

    for n, ix in enumerate(msg.instructions):
        if ix.program_id_index >= len(static):
            rep.problems.append(f"ix{n}: program id not in static keys")
            continue
        prog = static[ix.program_id_index]
        rep.programs.append(prog)
        data = bytes(ix.data)
        if prog not in policy.allowed_programs:
            rep.problems.append(f"ix{n}: program {prog} is not allow-listed")
            continue
        if prog == COMPUTE_BUDGET_PROGRAM:
            tag = data[0] if data else -1
            if tag == 2 and len(data) >= 5:
                cu_limit = _u32(data, 1)
            elif tag == 3 and len(data) >= 9:
                rep.compute_unit_price_micro = _u64(data, 1)
            elif tag in (1, 4) and len(data) >= 5:
                pass                                     # heap frame / loaded-accounts data size: no cost to us
            else:
                rep.problems.append(f"ix{n}: unknown compute-budget instruction {tag}")
            continue
        n_non_cb += 1
        if prog in SWAP_PROGRAMS:
            rep.swap_instructions += 1
            continue
        if prog == SYSTEM_PROGRAM:
            if len(data) >= 12 and _u32(data, 0) == 2:   # Transfer {lamports}
                src, dst, lamports = acct(ix, 0), acct(ix, 1), _u64(data, 4)
                if src is None or dst is None:
                    rep.problems.append(f"ix{n}: system transfer with unresolved accounts")
                elif src == wallet and dst == wsol_ata:
                    rep.wrap_lamports += lamports
                elif src == wallet:
                    rep.external_sol_outflow_lamports += lamports
            else:
                rep.problems.append(f"ix{n}: system instruction {(_u32(data, 0) if len(data) >= 4 else 'empty')} is not allowed")
            continue
        if prog in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            tag = data[0] if data else -1
            if tag == 17:                                # SyncNative
                if acct(ix, 0) != wsol_ata:
                    rep.problems.append(f"ix{n}: SyncNative on an account other than the wallet's wSOL account")
            elif tag == 9:                               # CloseAccount [account, destination, owner]
                if acct(ix, 2) == wallet and acct(ix, 1) != wallet:
                    rep.problems.append(f"ix{n}: CloseAccount sends the wallet's rent to {acct(ix, 1)}")
            else:
                rep.problems.append(f"ix{n}: top-level token instruction {tag} is not allowed (only SyncNative/CloseAccount)")
            continue
        if prog == ATA_PROGRAM:
            tag = data[0] if data else 0
            if tag not in (0, 1):
                rep.problems.append(f"ix{n}: associated-token instruction {tag} is not allowed")
                continue
            payer, ata, owner, mint, token_prog = (acct(ix, k) for k in (0, 1, 2, 3, 5))
            rec = {"payer": payer, "ata": ata, "owner": owner, "mint": mint}
            rep.ata_creates.append(rec)
            if None in (payer, ata, owner, mint, token_prog):
                rep.problems.append(f"ix{n}: associated-token create with unresolved accounts")
                continue
            if mint not in policy.allowed_mints:
                rep.problems.append(f"ix{n}: creates a token account for unauthorised mint {mint}")
            if payer == wallet:
                if owner != wallet:
                    rep.problems.append(f"ix{n}: wallet pays to create a token account owned by {owner}")
                elif ata != ata_address(owner, mint, token_prog):
                    rep.problems.append(f"ix{n}: token account {ata} is not the canonical ATA")
                rep.rent_lamports += ATA_RENT_LAMPORTS
            continue
    if cu_limit is None:
        cu_limit = min(MAX_CU, DEFAULT_CU_PER_INSTRUCTION * max(1, n_non_cb))
    rep.compute_unit_limit = cu_limit
    if rep.fee_payer == wallet:
        rep.priority_fee_lamports = math.ceil(rep.compute_unit_price_micro * cu_limit / 1_000_000)
    if rep.swap_instructions == 0:
        rep.problems.append("no Jupiter swap instruction in the transaction")
    if rep.priority_fee_lamports > policy.max_priority_fee_lamports:
        rep.problems.append(f"priority fee {rep.priority_fee_lamports} lamports exceeds {policy.max_priority_fee_lamports}")
    if rep.external_sol_outflow_lamports > policy.max_external_sol_outflow_lamports:
        rep.problems.append(f"{rep.external_sol_outflow_lamports} lamports leave the wallet to third parties "
                            f"(max {policy.max_external_sol_outflow_lamports})")
    if rep.rent_lamports > policy.max_rent_lamports:
        rep.problems.append(f"rent {rep.rent_lamports} lamports exceeds {policy.max_rent_lamports}")
    return rep


# ---------------------------------------------------------------------------------------------------- order fields
def _int(v) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def parse_expire_at(v) -> float | None:
    """`expireAt` (RFQ) as epoch seconds; accepts epoch s/ms numbers or ISO-8601 strings."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
        return x / 1000 if x > 1e11 else x
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def price_impact_bps(order: dict) -> float | None:
    if order.get("priceImpact") is not None:
        try:
            return abs(float(order["priceImpact"])) * 100       # percentage points -> bps
        except (TypeError, ValueError):
            return None
    if order.get("priceImpactPct") is not None:
        try:
            return abs(float(order["priceImpactPct"])) * 1e4    # deprecated decimal ratio
        except (TypeError, ValueError):
            return None
    return None


def verify_order(order: dict, exp: OrderExpectation, policy: OrderPolicy, block_height: int | None,
                 now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    p: list[str] = []
    if not isinstance(order, dict):
        return ["order response is not an object"]
    if order.get("errorCode") is not None or order.get("transaction") in (None, ""):
        return [f"no transaction: errorCode={order.get('errorCode')} router={order.get('router')} {order.get('errorMessage') or order.get('error') or ''}".strip()]
    if not order.get("requestId"):
        p.append("missing requestId")
    if order.get("taker") not in (None, exp.wallet):
        p.append(f"taker {order.get('taker')} is not the wallet")
    if order.get("inputMint") != exp.input_mint or order.get("outputMint") != exp.output_mint:
        p.append(f"mints {order.get('inputMint')}->{order.get('outputMint')} differ from the request {exp.input_mint}->{exp.output_mint}")
    if _int(order.get("inAmount")) != exp.in_amount_raw:
        p.append(f"inAmount {order.get('inAmount')} differs from the requested {exp.in_amount_raw}")
    if order.get("swapMode") not in (None, "ExactIn"):
        p.append(f"swapMode {order.get('swapMode')} (only ExactIn)")
    router = order.get("router") or order.get("swapType")
    if router not in policy.allowed_routers:
        p.append(f"router {router!r} is not allowed ({sorted(policy.allowed_routers)})")
    tv = order.get("transactionVersion")
    if tv is not None and _int(tv) != 0:
        p.append(f"transactionVersion {tv}: only v0 is accepted")
    out, floor = _int(order.get("outAmount")), _int(order.get("otherAmountThreshold"))
    if out is None or floor is None:
        p.append("missing outAmount / otherAmountThreshold")
    else:
        if floor > out:
            p.append("otherAmountThreshold above outAmount")
        if floor < exp.min_out_raw:
            p.append(f"minimum output {floor} is below our floor {exp.min_out_raw} (reference price x max total cost)")
    slip = order.get("slippageBps")
    if slip is not None and float(slip) > policy.max_slippage_bps:
        p.append(f"slippageBps {slip} exceeds {policy.max_slippage_bps}")
    fee = order.get("feeBps")
    if fee is None or float(fee) > policy.max_fee_bps:
        p.append(f"feeBps {fee} missing or above {policy.max_fee_bps}")
    if order.get("feeMint") not in (None, exp.input_mint, exp.output_mint):
        p.append(f"fee taken in {order.get('feeMint')}, which is neither leg of the swap")
    impact = price_impact_bps(order)
    if impact is None or impact > policy.max_price_impact_bps:
        p.append(f"price impact {impact} bps missing or above {policy.max_price_impact_bps}")
    if order.get("gasless") and not policy.allow_gasless:
        p.append("gasless order not allowed")
    if order.get("prioritizationFeePayer") in (None, exp.wallet) and (_int(order.get("prioritizationFeeLamports")) or 0) > policy.max_priority_fee_lamports:
        p.append(f"prioritizationFeeLamports {order.get('prioritizationFeeLamports')} exceeds {policy.max_priority_fee_lamports}")
    if order.get("rentFeePayer") in (None, exp.wallet) and (_int(order.get("rentFeeLamports")) or 0) > policy.max_rent_lamports:
        p.append(f"rentFeeLamports {order.get('rentFeeLamports')} exceeds {policy.max_rent_lamports}")
    if now - exp.received_ts > policy.max_quote_age_sec:
        p.append(f"order is {now - exp.received_ts:.1f}s old (max {policy.max_quote_age_sec})")
    lvbh = _int(order.get("lastValidBlockHeight"))
    if router == "jupiterz":
        exp_at = parse_expire_at(order.get("expireAt"))
        if exp_at is None:
            p.append("RFQ order without expireAt")
        elif exp_at - now < policy.min_rfq_seconds_remaining:
            p.append(f"RFQ quote expires in {exp_at - now:.1f}s (min {policy.min_rfq_seconds_remaining})")
        if not order.get("maker"):
            p.append("RFQ order without maker")
    if lvbh is None:
        if router != "jupiterz":
            p.append("missing lastValidBlockHeight")
    elif block_height is None:
        p.append("current block height unknown; cannot check expiry")
    elif lvbh - block_height < policy.min_blocks_remaining:
        p.append(f"only {lvbh - block_height} blocks of validity left (min {policy.min_blocks_remaining})")
    return p


def verify_signers(order: dict, rep: TxReport, wallet: str) -> list[str]:
    """Who else must sign, and who pays: only the RFQ maker or a gasless sponsor (the fee payer) may co-sign."""
    p = []
    others = [s for s in rep.signers if s != wallet]
    router = order.get("router") or order.get("swapType")
    for s in others:
        if router == "jupiterz" and s == order.get("maker"):
            continue
        if s == rep.fee_payer and order.get("gasless"):
            continue
        p.append(f"unexpected co-signer {s}")
    if rep.fee_payer != wallet and not (order.get("gasless") or router == "jupiterz"):
        p.append(f"fee payer {rep.fee_payer} is not the wallet on a non-gasless aggregator order")
    expected_prog = ROUTER_PROGRAMS.get(router)
    if expected_prog and expected_prog not in rep.programs:
        p.append(f"router {router} but its program {expected_prog} is not invoked")
    return p


def verify_all(order: dict, exp: OrderExpectation, order_policy: OrderPolicy, tx_policy: TxPolicy,
               block_height: int | None, alt_resolver: AltResolver | None = None, now: float | None = None) -> TxReport:
    """Order fields + transaction contents + signer set. Raises VerificationError listing every problem."""
    problems = verify_order(order, exp, order_policy, block_height, now)
    rep = TxReport()
    if order.get("transaction"):
        try:
            rep = inspect_transaction(order["transaction"], exp.wallet, tx_policy, alt_resolver)
        except VerificationError as e:
            problems += e.problems
        else:
            problems += rep.problems
            problems += verify_signers(order, rep, exp.wallet)
    rep.problems = problems
    if problems:
        raise VerificationError(problems)
    return rep
