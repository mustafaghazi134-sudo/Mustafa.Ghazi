"""Synthetic Jupiter Swap v2 orders and transactions for tests. Ephemeral keys only; nothing touches a network.

`build_tx` assembles a real v0 message with the shape a Metis or JupiterZ route has (compute budget, ATA create,
SOL wrap, the swap program, unwrap), and lets a test inject adversarial instructions.
"""
from __future__ import annotations

import base64
import struct
import time

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from tradebot.execution import tx_verify as V

P = Pubkey.from_string


def cu_limit(n: int) -> Instruction:
    return Instruction(P(V.COMPUTE_BUDGET_PROGRAM), bytes([2]) + struct.pack("<I", n), [])


def cu_price(micro: int) -> Instruction:
    return Instruction(P(V.COMPUTE_BUDGET_PROGRAM), bytes([3]) + struct.pack("<Q", micro), [])


def ata_create(payer: str, owner: str, mint: str, ata: str | None = None, token_prog: str = V.TOKEN_PROGRAM) -> Instruction:
    ata = ata or V.ata_address(owner, mint, token_prog)
    return Instruction(P(V.ATA_PROGRAM), bytes([1]), [
        AccountMeta(P(payer), True, True), AccountMeta(P(ata), False, True), AccountMeta(P(owner), False, False),
        AccountMeta(P(mint), False, False), AccountMeta(P(V.SYSTEM_PROGRAM), False, False), AccountMeta(P(token_prog), False, False)])


def sys_transfer(src: str, dst: str, lamports: int) -> Instruction:
    return Instruction(P(V.SYSTEM_PROGRAM), struct.pack("<IQ", 2, lamports),
                       [AccountMeta(P(src), True, True), AccountMeta(P(dst), False, True)])


def sync_native(acct: str) -> Instruction:
    return Instruction(P(V.TOKEN_PROGRAM), bytes([17]), [AccountMeta(P(acct), False, True)])


def close_account(acct: str, dest: str, owner: str) -> Instruction:
    return Instruction(P(V.TOKEN_PROGRAM), bytes([9]), [AccountMeta(P(acct), False, True), AccountMeta(P(dest), False, True),
                                                       AccountMeta(P(owner), True, False)])


def token_approve(src: str, delegate: str, owner: str, amount: int) -> Instruction:
    return Instruction(P(V.TOKEN_PROGRAM), bytes([4]) + struct.pack("<Q", amount),
                       [AccountMeta(P(src), False, True), AccountMeta(P(delegate), False, False), AccountMeta(P(owner), True, False)])


def swap_ix(wallet: str, program: str = V.JUPITER_V6_PROGRAM, extra_signer: str | None = None) -> Instruction:
    metas = [AccountMeta(P(wallet), True, True), AccountMeta(Keypair().pubkey(), False, True)]
    if extra_signer:
        metas.append(AccountMeta(P(extra_signer), True, True))
    return Instruction(P(program), b"\xe5\x17\xcb\x97\x7a\xe3\xad\x2a" + bytes(16), metas)


def build_tx(wallet: str, *, fee_payer: str | None = None, instructions: list[Instruction] | None = None,
             sol_in_lamports: int = 0, price_micro: int = 100_000, limit: int = 400_000, program: str = V.JUPITER_V6_PROGRAM,
             extra_signer: str | None = None, out_mint: str = V.USDC_MINT, blockhash: Hash | None = None) -> str:
    """A plausible Metis-style (or RFQ-style with `program`/`extra_signer`) v0 transaction, unsigned, base64."""
    payer = fee_payer or wallet
    if instructions is None:
        instructions = [cu_limit(limit), cu_price(price_micro), ata_create(wallet, wallet, out_mint)]
        if sol_in_lamports:
            wsol = V.ata_address(wallet, V.WSOL_MINT)
            instructions += [ata_create(wallet, wallet, V.WSOL_MINT), sys_transfer(wallet, wsol, sol_in_lamports), sync_native(wsol)]
        instructions.append(swap_ix(wallet, program, extra_signer))
        if sol_in_lamports:
            instructions.append(close_account(V.ata_address(wallet, V.WSOL_MINT), wallet, wallet))
    msg = MessageV0.try_compile(P(payer), instructions, [], blockhash or Hash.new_unique())
    tx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    return base64.b64encode(bytes(tx)).decode()


def order_response(wallet: str, *, input_mint: str = V.USDC_MINT, output_mint: str = V.WSOL_MINT, in_amount: int = 5_000_000,
                   out_amount: int = 31_000_000, min_out: int | None = None, router: str = "metis", lvbh: int = 1_000_200,
                   tx: str | None = None, **over) -> dict:
    """A /swap/v2/order response with taker set (field names as in openapi-spec/swap/v2/swap.yaml)."""
    o = {"mode": "manual", "inputMint": input_mint, "outputMint": output_mint, "inAmount": str(in_amount),
         "outAmount": str(out_amount), "otherAmountThreshold": str(min_out if min_out is not None else int(out_amount * 0.995)),
         "swapMode": "ExactIn", "slippageBps": 50, "priceImpact": -0.01, "feeBps": 2, "feeMint": input_mint,
         "signatureFeeLamports": 5000, "prioritizationFeeLamports": 40_000, "rentFeeLamports": 0, "router": router,
         "transactionVersion": 0, "lastValidBlockHeight": str(lvbh), "gasless": False, "requestId": "req-" + str(time.time_ns()),
         "taker": wallet, "transaction": tx if tx is not None else build_tx(wallet)}
    o.update(over)
    return o


# ---------------------------------------------------------------------------------------------------- fake chain
def token_account_data(mint: str, owner: str, amount: int) -> list:
    raw = bytes(P(mint)) + bytes(P(owner)) + struct.pack("<Q", amount) + bytes(165 - 72)
    return [base64.b64encode(raw).decode(), "base64"]


class FakeChain:
    """Answers the RPC methods the adapter and signer use. Balances are per address: lamports and, for token
    accounts, (mint, amount). `swap_effect` maps mint -> raw delta applied in simulation (WSOL = native lamports)."""

    def __init__(self, wallet: str, mints: list[str]):
        self.wallet = wallet
        self.block_height = 1_000_000
        self.blockhash_valid = True
        self.lamports = {wallet: 2_000_000_000}
        self.tokens: dict[str, tuple[str, int]] = {}
        for m in mints:
            if m != V.WSOL_MINT:
                self.tokens[V.ata_address(wallet, m)] = (m, 0)
        self.swap_effect: dict[str, int] = {}
        self.sim_err = None
        self.statuses: dict[str, dict | None] = {}
        self.transactions: dict[str, dict] = {}
        self.down: set[str] = set()
        self.calls: list[str] = []

    def set_token(self, mint: str, amount: int) -> None:
        self.tokens[V.ata_address(self.wallet, mint)] = (mint, amount)

    def _account(self, addr: str, token_delta: dict | None = None, native_delta: int = 0) -> dict | None:
        if addr == self.wallet:
            return {"lamports": self.lamports[addr] + native_delta, "data": ["", "base64"], "owner": V.SYSTEM_PROGRAM}
        if addr in self.tokens:
            mint, amt = self.tokens[addr]
            amt += (token_delta or {}).get(mint, 0)
            return {"lamports": V.ATA_RENT_LAMPORTS, "data": token_account_data(mint, self.wallet, amt), "owner": V.TOKEN_PROGRAM}
        return None

    def rpc(self, method: str, params: list) -> dict:
        self.calls.append(method)
        if method in self.down or "*" in self.down:
            raise ConnectionError(f"rpc {method} unavailable")
        if method == "getBlockHeight":
            return self.block_height
        if method == "isBlockhashValid":
            return {"value": self.blockhash_valid}
        if method == "getMultipleAccounts":
            return {"value": [self._account(a) for a in params[0]]}
        if method == "simulateTransaction":
            addrs = params[1]["accounts"]["addresses"]
            native = self.swap_effect.get(V.WSOL_MINT, 0)
            return {"value": {"err": self.sim_err, "logs": ["Program log: ok"], "unitsConsumed": 120_000,
                              "accounts": [self._account(a, self.swap_effect, native) for a in addrs]}}
        if method == "getSignatureStatuses":
            return {"value": [self.statuses.get(s) for s in params[0]]}
        if method == "getTransaction":
            return self.transactions.get(params[0])
        if method == "getBalance":
            return {"value": self.lamports[self.wallet]}
        if method == "getTokenAccountsByOwner":
            out = []
            if params[1].get("programId") != V.TOKEN_PROGRAM:
                return {"value": []}
            for addr, (mint, amt) in self.tokens.items():
                out.append({"pubkey": addr, "account": {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"amount": str(amt)}}}}}})
            return {"value": out}
        if method == "getAccountInfo":
            return {"value": None}
        raise NotImplementedError(method)

    def land(self, signature: str, *, err=None, in_mint: str, in_raw: int, out_mint: str, out_raw: int, fee: int = 45_000) -> None:
        """Record a landed transaction with pre/post balances for the wallet (as getTransaction jsonParsed returns)."""
        self.statuses[signature] = {"confirmationStatus": "confirmed", "err": err, "slot": 1}
        pre, post = [], []
        for i, (mint, d) in enumerate(((in_mint, -in_raw), (out_mint, out_raw))):
            if mint == V.WSOL_MINT:
                continue
            pre.append({"accountIndex": i + 1, "mint": mint, "owner": self.wallet, "uiTokenAmount": {"amount": "1000000000000"}})
            post.append({"accountIndex": i + 1, "mint": mint, "owner": self.wallet,
                         "uiTokenAmount": {"amount": str(1_000_000_000_000 + (0 if err else d))}})
        sol_change = 0 if err else ((-in_raw if in_mint == V.WSOL_MINT else 0) + (out_raw if out_mint == V.WSOL_MINT else 0))
        self.transactions[signature] = {"slot": 1, "meta": {"err": err, "fee": fee, "preBalances": [10_000_000_000],
                                        "postBalances": [10_000_000_000 + sol_change - fee],
                                        "preTokenBalances": pre, "postTokenBalances": post},
                                        "transaction": {"message": {"accountKeys": [{"pubkey": self.wallet, "signer": True}]}}}
