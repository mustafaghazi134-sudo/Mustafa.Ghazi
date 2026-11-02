"""Adversarial tests for the pre-signing verifier and the simulation check. Each case mutates one thing in an
otherwise valid Jupiter order or transaction and asserts that signing would be refused."""
from __future__ import annotations

import base64
import struct
import time

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageAddressTableLookup, MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from jupiter_fixtures import (FakeChain, ata_create, build_tx, close_account, cu_limit, cu_price, order_response, swap_ix,
                              sync_native, sys_transfer, token_approve)
from tradebot.execution import tx_verify as V
from tradebot.execution.simulation import check_deltas, simulate_deltas

W = str(Keypair().pubkey())
ATTACKER = str(Keypair().pubkey())
MINTS = frozenset({V.USDC_MINT, V.WSOL_MINT})
TXP = V.TxPolicy(allowed_mints=MINTS)
ORP = V.OrderPolicy()
NOW = 1_800_000_000.0
BH = 1_000_000


def exp(**over):
    e = V.OrderExpectation(W, V.USDC_MINT, V.WSOL_MINT, 5_000_000, 30_800_000, NOW)
    for k, v in over.items():
        setattr(e, k, v)
    return e


def check(order, e=None, bh=BH, now=NOW, resolver=None):
    return V.verify_all(order, e or exp(), ORP, TXP, bh, resolver, now=now)


def problems(order, **kw):
    with pytest.raises(V.VerificationError) as ei:
        check(order, **kw)
    return " | ".join(ei.value.problems)


def test_clean_order_passes_and_reports_costs():
    rep = check(order_response(W, tx=build_tx(W, out_mint=V.WSOL_MINT)))
    assert rep.version == "v0" and rep.wallet_signer_index == 0 and rep.swap_instructions == 1
    assert rep.priority_fee_lamports == 40_000 and rep.signature_fee_lamports == 5_000 and rep.rent_lamports == V.ATA_RENT_LAMPORTS


# ------------------------------------------------------------------ order fields
@pytest.mark.parametrize("over, needle", [
    ({"taker": ATTACKER}, "taker"),
    ({"outputMint": V.USDC_MINT, "inputMint": V.WSOL_MINT}, "mints"),
    ({"inAmount": "50000000"}, "inAmount"),
    ({"otherAmountThreshold": "20000000"}, "below our floor"),
    ({"otherAmountThreshold": "99999999999"}, "above outAmount"),
    ({"feeBps": 80}, "feeBps"),
    ({"feeMint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}, "neither leg"),
    ({"priceImpact": -1.5}, "price impact"),
    ({"priceImpact": None}, "price impact"),
    ({"router": "dflow"}, "router"),
    ({"router": "okx"}, "router"),
    ({"transactionVersion": 1}, "only v0"),
    ({"lastValidBlockHeight": str(BH + 5)}, "blocks of validity"),
    ({"lastValidBlockHeight": None}, "lastValidBlockHeight"),
    ({"slippageBps": 500}, "slippageBps"),
    ({"swapMode": "ExactOut"}, "ExactIn"),
    ({"requestId": ""}, "requestId"),
    ({"prioritizationFeeLamports": 5_000_000}, "prioritizationFeeLamports"),
    ({"rentFeeLamports": 50_000_000}, "rentFeeLamports"),
])
def test_order_field_attacks_are_refused(over, needle):
    assert needle in problems(order_response(W, tx=build_tx(W, out_mint=V.WSOL_MINT), **over))


def test_unbuilt_order_and_stale_quote_are_refused():
    assert "no transaction" in problems(order_response(W, transaction="", errorCode=1, errorMessage="Insufficient funds"))
    assert "old" in problems(order_response(W, tx=build_tx(W, out_mint=V.WSOL_MINT)), now=NOW + 30)
    assert "block height unknown" in problems(order_response(W, tx=build_tx(W, out_mint=V.WSOL_MINT)), bh=None)


def test_rfq_requires_live_expiry_and_only_the_maker_may_cosign():
    maker = str(Keypair().pubkey())
    tx = build_tx(W, program=V.JUPITERZ_ORDER_ENGINE, extra_signer=maker, out_mint=V.WSOL_MINT)
    good = order_response(W, tx=tx, router="jupiterz", maker=maker, expireAt=str(NOW + 30))
    rep = check(good)
    assert len(rep.signers) == 2
    assert "expires in" in problems(dict(good, expireAt=str(NOW + 1)))
    assert "without expireAt" in problems(dict(good, expireAt=None))
    stranger = build_tx(W, program=V.JUPITERZ_ORDER_ENGINE, extra_signer=str(Keypair().pubkey()), out_mint=V.WSOL_MINT)
    assert "unexpected co-signer" in problems(dict(good, transaction=stranger))
    assert "is not invoked" in problems(order_response(W, tx=build_tx(W, out_mint=V.WSOL_MINT), router="jupiterz", maker=maker,
                                                       expireAt=str(NOW + 30)))


# ------------------------------------------------------------------ transaction contents
def _tx_with(extra: list[Instruction], payer: str | None = None) -> str:
    base = [cu_limit(300_000), cu_price(50_000), ata_create(W, W, V.WSOL_MINT), swap_ix(W)]
    return build_tx(W, instructions=base + extra, fee_payer=payer)


@pytest.mark.parametrize("extra, needle", [
    ([Instruction(Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"), b"hi", [])], "not allow-listed"),
    ([sys_transfer(W, ATTACKER, 5_000_000)], "leave the wallet to third parties"),
    ([token_approve(V.ata_address(W, V.USDC_MINT), ATTACKER, W, 10**12)], "token instruction 4"),
    ([close_account(V.ata_address(W, V.WSOL_MINT), ATTACKER, W)], "CloseAccount sends"),
    ([ata_create(W, ATTACKER, V.USDC_MINT)], "owned by"),
    ([ata_create(W, W, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")], "unauthorised mint"),
    ([ata_create(W, W, V.USDC_MINT, ata=str(Keypair().pubkey()))], "canonical ATA"),
    ([cu_price(10_000_000)], "priority fee"),
    ([sync_native(str(Keypair().pubkey()))], "SyncNative"),
    ([Instruction(Pubkey.from_string(V.SYSTEM_PROGRAM), struct.pack("<I", 4), [AccountMeta(Keypair().pubkey(), False, True),
                                                                               AccountMeta(Pubkey.from_string(W), True, False)])],
     "system instruction 4"),                                                     # AdvanceNonceAccount (durable nonce)
    ([Instruction(Pubkey.from_string(V.COMPUTE_BUDGET_PROGRAM), bytes([9]), [])], "unknown compute-budget"),
])
def test_instruction_attacks_are_refused(extra, needle):
    assert needle in problems(order_response(W, tx=_tx_with(extra)))


def test_signer_and_payer_attacks_are_refused():
    assert "fee payer" in problems(order_response(W, tx=_tx_with([], payer=ATTACKER)))
    other = str(Keypair().pubkey())
    assert "wallet is not a required signer" in problems(order_response(W, tx=build_tx(other, out_mint=V.WSOL_MINT)))
    assert "unexpected co-signer" in problems(order_response(W, tx=build_tx(W, extra_signer=str(Keypair().pubkey()), out_mint=V.WSOL_MINT)))
    no_swap = build_tx(W, instructions=[cu_limit(200_000), ata_create(W, W, V.WSOL_MINT)])
    assert "no Jupiter swap" in problems(order_response(W, tx=no_swap))


def test_gasless_sponsor_may_pay_but_not_take():
    sponsor = str(Keypair().pubkey())
    o = order_response(W, tx=build_tx(W, fee_payer=sponsor, out_mint=V.WSOL_MINT), gasless=True)
    assert check(o).fee_payer == sponsor


def test_malformed_legacy_v1_and_oversized_transactions_are_refused():
    assert "base64" in problems(order_response(W, tx="%%%not-base64%%%"))
    legacy = Message.new_with_blockhash([swap_ix(W)], Pubkey.from_string(W), Hash.new_unique())
    ltx = VersionedTransaction.populate(legacy, [Signature.default()])
    assert "only v0" in problems(order_response(W, tx=base64.b64encode(bytes(ltx)).decode()))
    raw = bytearray(base64.b64decode(build_tx(W, out_mint=V.WSOL_MINT)))
    raw[1 + 64] = 0x81                                              # version prefix byte -> v1
    assert "does not decode" in problems(order_response(W, tx=base64.b64encode(bytes(raw)).decode()))
    assert "1232" in problems(order_response(W, tx=base64.b64encode(bytes(1300)).decode()))


def test_lookup_tables_must_resolve():
    table = Keypair().pubkey()
    msg = MessageV0.try_compile(Pubkey.from_string(W), [swap_ix(W)], [], Hash.new_unique())
    lk = MessageAddressTableLookup(table, bytes([0]), bytes([]))
    msg2 = MessageV0(msg.header, msg.account_keys, msg.recent_blockhash, msg.instructions, [lk])
    tx = base64.b64encode(bytes(VersionedTransaction.populate(msg2, [Signature.default()]))).decode()
    assert "no resolver" in problems(order_response(W, tx=tx))
    assert "could not be resolved" in problems(order_response(W, tx=tx), resolver=lambda t: (_ for _ in ()).throw(ValueError("gone")))
    assert "has no index" in problems(order_response(W, tx=tx), resolver=lambda t: [])
    check(order_response(W, tx=tx), resolver=lambda t: [str(Keypair().pubkey())])     # resolvable: passes


# ------------------------------------------------------------------ simulation
def _sim(effect: dict, err=None):
    chain = FakeChain(W, sorted(MINTS) + ["cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"])
    chain.set_token(V.USDC_MINT, 50_000_000)
    chain.set_token("cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", 10_000)
    chain.swap_effect, chain.sim_err = effect, err
    return simulate_deltas(chain.rpc, build_tx(W, out_mint=V.WSOL_MINT), W, sorted(MINTS) + ["cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"])


@pytest.mark.parametrize("effect, err, needle", [
    ({V.USDC_MINT: -5_000_000, V.WSOL_MINT: 31_000_000}, None, None),
    ({V.USDC_MINT: -5_000_000, V.WSOL_MINT: 31_000_000}, {"InstructionError": [3, {"Custom": 6001}]}, "simulation failed"),
    ({V.USDC_MINT: -9_000_000, V.WSOL_MINT: 31_000_000}, None, "more than the order"),
    ({V.USDC_MINT: -5_000_000, V.WSOL_MINT: 20_000_000}, None, "below the minimum"),
    ({V.USDC_MINT: -5_000_000, V.WSOL_MINT: 31_000_000, "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": -10_000}, None, "unrelated balance"),
])
def test_simulation_measures_real_wallet_changes(effect, err, needle):
    p = check_deltas(_sim(effect, err), V.USDC_MINT, V.WSOL_MINT, 5_000_000, 30_900_000, 5_000_000)
    assert (p == []) if needle is None else (needle in " ".join(p))


def test_simulation_caps_sol_spent_on_fees_for_token_swaps():
    chain = FakeChain(W, [V.USDC_MINT, V.WSOL_MINT, "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"])
    chain.set_token(V.USDC_MINT, 50_000_000)
    chain.swap_effect = {V.USDC_MINT: -5_000_000, "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": 5_000, V.WSOL_MINT: -50_000_000}
    sim = simulate_deltas(chain.rpc, build_tx(W), W, [V.USDC_MINT, "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"])
    p = check_deltas(sim, V.USDC_MINT, "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", 5_000_000, 4_900, 5_000_000)
    assert any("SOL falls" in x for x in p)


def test_expire_at_parsing():
    assert V.parse_expire_at("1800000000") == 1_800_000_000
    assert V.parse_expire_at(1_800_000_000_000) == 1_800_000_000
    assert abs(V.parse_expire_at("2027-01-15T08:00:00Z") - 1_799_995_200) < 86400 * 400
    assert V.parse_expire_at("soon") is None
    assert time.time() > 0
