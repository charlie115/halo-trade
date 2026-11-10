#!/usr/bin/env python
"""Create the test wallets, issue the trade-only grant and deposit. Injective testnet only.

Wallets go to data/testnet_wallets.json (mode 600, gitignored). Only addresses are printed.

The trader wallet gets the production grant list plus one named extra, the test-only limit-order
grant that testnet_cases.py needs. The server's own grant check calls that extra unexpected, so
the trader wallet can run the cases but cannot be onboarded through the service: use a fresh wallet
for that.

    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/app" <image> \
        python scripts/testnet_setup.py [--market-ticker "BTC/USDC PERP"]

Exit status: 0 ready, 2 waiting for funds.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from pyinjective.proto.injective.exchange.v2 import authz_pb2 as exchange_authz_pb2
from pyinjective.wallet import PrivateKey

from halo_trade.chain.base import ChainUnavailable, SignedTx, TxResult, TxStatus
from halo_trade.chain.grants import (
    DEFAULT_GRANT_TTL_S,
    GrantSpec,
    build_grant_msg,
    build_grant_msgs,
    fetch_grant_records,
    verify_records,
)
from halo_trade.chain.injective import TESTNET_CHAIN_ID, InjectiveChain, from_chain

ROOT = Path(__file__).resolve().parent.parent
WALLET_FILE = ROOT / "data" / "testnet_wallets.json"
DEFAULT_TICKER = "BTC/USDC PERP"
SUBACCOUNT_NONCE = 1
TX_TIMEOUT_BLOCKS = 40
COLLAR = Decimal("0.003")

MIN_INJ = {"grantee": Decimal("0.5"), "trader": Decimal("0.5"), "maker": Decimal("0.5")}
MIN_QUOTE = {"trader": Decimal("50"), "maker": Decimal("300")}
INJ_DECIMALS = 18
GAS_GRANT_TX = 400_000
GAS_DEPOSIT_TX = 350_000

# Test-only and testnet-only. Cases b, c and d rest limit orders through the grant; the service
# never does, so this is deliberately not in grants.REQUIRED_GRANTS. The maker wallet signs its
# own orders and needs no grant.
TESTNET_ONLY_LIMIT_ORDER_GRANT = GrantSpec(
    authz_type_url="/injective.exchange.v2.CreateDerivativeLimitOrderAuthz",
    msg_type_url="/injective.exchange.v2.MsgCreateDerivativeLimitOrder",
    authz_class=exchange_authz_pb2.CreateDerivativeLimitOrderAuthz,
)


def require_testnet(network: str | None = None) -> None:
    """Hard stop for anything but testnet, before any key is read or request made."""
    selected = network if network is not None else os.environ.get("HALO_NETWORK", "testnet")
    if selected != "testnet":
        raise SystemExit(f"refused: this script runs on testnet only, HALO_NETWORK={selected!r}")


async def require_testnet_chain(chain: InjectiveChain) -> None:
    """Second check, against what the node itself reports."""
    block = await chain._rpc(chain._ensure_client().fetch_latest_block())
    chain_id = block["block"]["header"]["chainId"]
    if chain_id != TESTNET_CHAIN_ID or chain.is_mainnet:
        raise SystemExit(f"refused: connected chain is {chain_id!r}, expected {TESTNET_CHAIN_ID!r}")


def load_or_create_wallets(path: Path = WALLET_FILE) -> dict[str, str]:
    """Hex private keys by role. Existing wallets are kept so funded addresses stay valid."""
    if path.exists():
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
        return json.loads(path.read_text(encoding="ascii"))["keys"]

    path.parent.mkdir(parents=True, exist_ok=True)
    keys = {role: PrivateKey.generate()[1].to_hex() for role in ("grantee", "trader", "maker")}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        json.dump({"network": "testnet", "keys": keys}, handle)
    return keys


def private_key(hex_key: str) -> PrivateKey:
    return PrivateKey.from_hex(hex_key)


def address_of(hex_key: str) -> str:
    return private_key(hex_key).to_public_key().to_address().to_acc_bech32()


def subaccount_of(hex_key: str, nonce: int = SUBACCOUNT_NONCE) -> str:
    return private_key(hex_key).to_public_key().to_address().get_subaccount_id(nonce)


def make_testnet_chain(keys: dict[str, str], ticker: str) -> InjectiveChain:
    return InjectiveChain(
        network="testnet",
        grantee_private_key=keys["grantee"],
        market_ticker=ticker,
        tx_timeout_blocks=TX_TIMEOUT_BLOCKS,
        collar=COLLAR,
        allow_mainnet=False,
    )


async def send_and_wait(chain: InjectiveChain, tx: SignedTx, poll_s: float = 1.0) -> TxResult:
    """Broadcast once, then poll. `lookup` always ends at OK, FAILED or EXPIRED."""
    await chain.broadcast(tx)
    while True:
        try:
            result = await chain.lookup(tx)
        except ChainUnavailable:
            await asyncio.sleep(poll_s)
            continue
        if result.status is not TxStatus.PENDING:
            return result
        await asyncio.sleep(poll_s)


async def bank_balance(chain: InjectiveChain, address: str, denom: str) -> int:
    response = await chain._rpc(chain._ensure_client().fetch_bank_balance(address=address, denom=denom))
    return int((response.get("balance") or {}).get("amount") or 0)


async def subaccount_total(chain: InjectiveChain, hex_key: str, quote_denom: str) -> Decimal:
    response = await chain._rpc(
        chain._ensure_client().fetch_subaccount_deposit(subaccount_id=subaccount_of(hex_key), denom=quote_denom)
    )
    return from_chain((response.get("deposits") or {}).get("totalBalance"))


async def funding_shortfalls(chain: InjectiveChain, keys: dict[str, str], quote_denom: str, quote_decimals: int) -> list[str]:
    """Quote already deposited counts, so this still passes after setup has moved it."""
    lines = []
    for role, hex_key in keys.items():
        address = address_of(hex_key)
        inj = Decimal(await bank_balance(chain, address, "inj")) / Decimal(10) ** INJ_DECIMALS
        if inj < MIN_INJ[role]:
            lines.append(f"{role:8s} {address}  needs {MIN_INJ[role]} INJ (has {inj})")
        if role in MIN_QUOTE:
            quote = Decimal(await bank_balance(chain, address, quote_denom)) / Decimal(10) ** quote_decimals
            quote += await subaccount_total(chain, hex_key, quote_denom)
            if quote < MIN_QUOTE[role]:
                lines.append(f"{role:8s} {address}  needs {MIN_QUOTE[role]} of {quote_denom} (has {quote})")
    return lines


def grant_messages(keys: dict[str, str], market_id: str, expires_unix_s: int) -> list[Any]:
    """The production grants, then the named test-only extra, as one transaction's messages."""
    parties = (address_of(keys["trader"]), address_of(keys["grantee"]), subaccount_of(keys["trader"]), market_id, expires_unix_s)
    return [*build_grant_msgs(*parties), build_grant_msg(TESTNET_ONLY_LIMIT_ORDER_GRANT, *parties)]


async def issue_grant(chain: InjectiveChain, keys: dict[str, str], market_id: str) -> TxResult:
    await require_testnet_chain(chain)  # the test-only extra must never reach any other chain
    messages = grant_messages(keys, market_id, int(time.time()) + DEFAULT_GRANT_TTL_S)
    tx = await chain.sign_wallet_tx(private_key(keys["trader"]), messages, GAS_GRANT_TX)
    return await send_and_wait(chain, tx)


async def grant_problems(chain: InjectiveChain, keys: dict[str, str], market_id: str) -> list[str]:
    """What keeps the test wallet from running the cases, empty when it is ready.

    `chain.check_grant` cannot answer this: it reports the named extra as `unexpected_grant`,
    which is right for production. Here the same check runs on everything else, so any other
    stray grant still fails, and the extra only has to be there and unexpired.
    """
    records = await chain._rpc(
        fetch_grant_records(chain._ensure_client().stubAuthz, address_of(keys["trader"]), chain.grantee_address)
    )
    now_ms = chain._clock_ms()
    extra_url = TESTNET_ONLY_LIMIT_ORDER_GRANT.authz_type_url
    status = verify_records(
        [record for record in records if record.authz_type_url != extra_url],
        subaccount_of(keys["trader"]),
        market_id,
        now_ms,
    )
    extra_live = any(
        record.authz_type_url == extra_url and record.expires_ms is not None and record.expires_ms > now_ms
        for record in records
    )
    return [*status.missing, *([] if extra_live else [TESTNET_ONLY_LIMIT_ORDER_GRANT.msg_type_url])]


async def deposit(chain: InjectiveChain, hex_key: str, quote_denom: str, quote_decimals: int, amount: Decimal) -> TxResult:
    message = chain._composer.msg_deposit(
        sender=address_of(hex_key),
        subaccount_id=subaccount_of(hex_key),
        amount=int(amount * Decimal(10) ** quote_decimals),
        denom=quote_denom,
    )
    tx = await chain.sign_wallet_tx(private_key(hex_key), [message], GAS_DEPOSIT_TX)
    return await send_and_wait(chain, tx)


async def run(ticker: str) -> int:
    keys = load_or_create_wallets()
    print("wallets (addresses only):")
    for role, hex_key in keys.items():
        print(f"  {role:8s} {address_of(hex_key)}  subaccount {subaccount_of(hex_key)}")

    chain = make_testnet_chain(keys, ticker)
    try:
        await require_testnet_chain(chain)
        info = await chain.market()
        print(f"market: {info.ticker} {info.market_id} quote {info.quote_denom}")

        shortfalls = await funding_shortfalls(chain, keys, info.quote_denom, chain._quote_decimals)
        if shortfalls:
            print("waiting for funds:")
            for line in shortfalls:
                print(f"  {line}")
            return 2

        problems = await grant_problems(chain, keys, info.market_id)
        if not problems:
            print("grants (production list + test-only limit order): already in place")
        else:
            result = await issue_grant(chain, keys, info.market_id)
            print(f"grants (production list + test-only limit order): {result.status.value} code={result.code}")
            if result.status is not TxStatus.OK:
                return 1
            problems = await grant_problems(chain, keys, info.market_id)
            print(f"grants verified on chain: {not problems} missing={problems}")
            if problems:
                return 1

        for role in ("trader", "maker"):
            already = await subaccount_total(chain, keys[role], info.quote_denom)
            if already >= MIN_QUOTE[role]:
                print(f"deposit {role}: already {already}")
                continue
            result = await deposit(chain, keys[role], info.quote_denom, chain._quote_decimals, MIN_QUOTE[role] - already)
            print(f"deposit {role}: {result.status.value} code={result.code}")
            if result.status is not TxStatus.OK:
                return 1
        return 0
    finally:
        await chain.aclose()


def main() -> None:
    require_testnet()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-ticker", default=DEFAULT_TICKER)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.market_ticker)))


if __name__ == "__main__":
    main()
