"""Offline tests for the Injective backend, against a fake client with recorded response shapes."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from google.protobuf import any_pb2, timestamp_pb2
from pyinjective.proto.cosmos.authz.v1beta1 import authz_pb2, tx_pb2 as authz_tx_pb2
from pyinjective.proto.cosmos.tx.v1beta1 import tx_pb2 as cosmos_tx_pb2
from pyinjective.proto.injective.exchange.v2 import order_pb2, tx_pb2 as exchange_tx_pb2
from pyinjective.wallet import PrivateKey

from halo_trade.chain import grants
from halo_trade.chain.base import (
    ChainUnavailable,
    OrderKind,
    OrderRequest,
    RestingOrder,
    Side,
    SignedTx,
    TxStatus,
)
from halo_trade.chain.factory import make_chain
from halo_trade.chain.injective import (
    CANCEL_CONDITIONAL_MARKET_MASK,
    EXPIRY_MARGIN_BLOCKS,
    GAS_ANTE,
    GAS_MARKET_ORDER,
    GAS_MSG_EXEC,
    GranteeAccountNotFound,
    InFlightTx,
    InjectiveChain,
    MainnetRefused,
    classify_conditional,
    conditional_order_type,
    gas_limit_for,
    load_grantee_key,
    predicted_funding_rate,
    record_response_height,
    round_trigger,
    round_worst_price,
    trigger_is_above_mark,
)

GRANTEE_KEY = "11" * 32
GRANTER_KEY = PrivateKey.from_hex("22" * 32)
GRANTER = GRANTER_KEY.to_public_key().to_address().to_acc_bech32()
SUBACCOUNT = GRANTER_KEY.to_public_key().to_address().get_subaccount_id(1)
MARKET_ID = "0x" + "ab" * 32
QUOTE = "erc20:0x0C382e685bbeeFE5d3d9C29e29E341fEE8E84C5d"
E18 = 10**18
TIMEOUT_BLOCKS = 40


def chain_value(value: str) -> str:
    return str(int(Decimal(value) * E18))


NOW_BLOCK_MS = 1_794_152_000_000


def rpc_error(code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata(), details="x")


class FakeAuth:
    def __init__(self, client):
        self.client = client

    async def fetch_account(self, address):
        self.client.account_reads += 1
        if self.client.account_error:
            raise self.client.account_error
        return {
            "account": {
                "@type": "/injective.types.v1beta1.EthAccount",
                "baseAccount": {"address": address, "accountNumber": "7", "sequence": str(self.client.sequence)},
            }
        }


class FakeTxApi:
    def __init__(self, client):
        self.client = client

    async def fetch_tx(self, hash):
        self.client.fetched_hashes.append(hash)
        if self.client.tx_error:
            raise self.client.tx_error
        return {"txResponse": self.client.tx_response}


class FakeGrantsStub:
    def __init__(self):
        self.pages = [SimpleNamespace(grants=[], pagination=SimpleNamespace(next_key=b""))]

    async def Grants(self, request, metadata=None):
        return self.pages.pop(0)


class FakeOracleStub:
    async def OraclePrice(self, request):
        seconds = NOW_BLOCK_MS // 1000
        return SimpleNamespace(price_pair_state=SimpleNamespace(pair_price=chain_value("100.5"), base_timestamp=seconds, quote_timestamp=seconds))


def raw_market():
    return {
        "market": {
            "ticker": "BTC/USDC PERP",
            "marketId": MARKET_ID,
            "quoteDenom": QUOTE,
            "status": "Active",
            "oracleType": "ChainlinkDataStreams",
            "oracleBase": "0xbase",
            "oracleQuote": "0xquote",
            "minPriceTickSize": chain_value("1"),
            "minQuantityTickSize": chain_value("0.0001"),
            "minNotional": chain_value("1"),
            "takerFeeRate": chain_value("0.0005"),
            "makerFeeRate": "0",
            "initialMarginRatio": chain_value("0.02"),
            "quoteDecimals": 6,
        },
        "perpetualInfo": {
            "marketInfo": {
                "hourlyFundingRateCap": chain_value("0.000625"),
                "hourlyInterestRate": chain_value("0.00000416666"),
                "nextFundingTimestamp": "1794153600",
                "fundingInterval": "3600",
            },
            "fundingInfo": {
                "cumulativeFunding": chain_value("10"),
                "cumulativePrice": "0",
                "lastTimestamp": "1794150000",
            },
        },
        "markPrice": chain_value("100"),
    }


class FakeClient:
    def __init__(self):
        self.height = 1000
        self.block_ms = NOW_BLOCK_MS
        self.heights_read = 0
        self.sequence = 5
        self.account_reads = 0
        self.account_error = None
        self.tx_error = None
        self.tx_response = {"code": 0, "height": "1001", "rawLog": ""}
        self.fetched_hashes = []
        self.broadcasts = []
        self.broadcast_result = {"txResponse": {"code": 0}}
        self.broadcast_error = None
        self.auth_api = FakeAuth(self)
        self.tx_api = FakeTxApi(self)
        self.stubAuthz = FakeGrantsStub()
        self.position_state = None
        self.deposits = {"availableBalance": chain_value("80"), "totalBalance": chain_value("100")}
        self.conditional_orders = []
        self.inj_amount = str(10**18)
        self.response_heights = {}
        self.book = {
            "buysPriceLevel": [
                {"p": chain_value("99.9"), "q": chain_value("2")},
                {"p": chain_value("90"), "q": chain_value("50")},
            ],
            "sellsPriceLevel": [{"p": chain_value("100.1"), "q": chain_value("3")}],
        }

    async def fetch_latest_block(self):
        self.heights_read += 1
        stamp = datetime.fromtimestamp(self.block_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return {"block": {"header": {"height": str(self.height), "chainId": "injective-888", "time": stamp}}}

    async def fetch_chain_derivative_markets(self, status=None, market_ids=None, with_mid_price_and_tob=None):
        return {"markets": [raw_market()]}

    async def fetch_chain_derivative_market(self, market_id):
        self.note_height("market")
        return {"market": raw_market()}

    def note_height(self, query):
        if query in self.response_heights:
            record_response_height(self.response_heights[query])

    async def fetch_chain_derivative_orderbook(self, market_id, limit_cumulative_notional=None, pagination=None):
        return self.book

    async def fetch_subaccount_deposit(self, subaccount_id, denom):
        self.note_height("deposit")
        return {"deposits": self.deposits}

    async def fetch_chain_subaccount_position_in_market(self, subaccount_id, market_id):
        self.note_height("position")
        return {"state": self.position_state} if self.position_state else {"riskMode": "RISK_MODE_ISOLATED"}

    async def fetch_trader_derivative_conditional_orders(self, subaccount_id, market_id):
        self.note_height("conditional")
        return {"orders": self.conditional_orders}

    async def current_chain_gas_price(self):
        return 160_000_000

    async def fetch_bank_balance(self, address, denom):
        return {"balance": {"denom": denom, "amount": self.inj_amount}}

    async def broadcast_tx_sync_mode(self, tx_bytes):
        self.broadcasts.append(tx_bytes)
        if self.broadcast_error:
            raise self.broadcast_error
        return self.broadcast_result

    async def simulate(self, tx_bytes):
        return {"gasInfo": {"gasUsed": "1"}}


def make(client=None, network="testnet", allow_mainnet=False, now_ms=1_794_152_000_000):
    backend = InjectiveChain(
        network=network,
        grantee_private_key=GRANTEE_KEY,
        market_ticker="BTC/USDC PERP",
        tx_timeout_blocks=TIMEOUT_BLOCKS,
        collar=Decimal("0.005"),
        allow_mainnet=allow_mainnet,
        client=client or FakeClient(),
        clock_ms=lambda: now_ms,
    )
    backend._oracle_stub = FakeOracleStub()
    return backend


def market_order(**overrides):
    fields = dict(
        kind=OrderKind.MARKET,
        side=Side.LONG,
        quantity=Decimal("0.01"),
        worst_price=Decimal("100.5"),
        cid="entry",
        reduce_only=False,
        margin=Decimal("1"),
    )
    fields.update(overrides)
    return OrderRequest(**fields)


def stop_order(**overrides):
    fields = dict(
        kind=OrderKind.STOP,
        side=Side.SHORT,
        quantity=Decimal("0.01"),
        worst_price=Decimal("96"),
        cid="stop",
        reduce_only=True,
        trigger_price=Decimal("98"),
    )
    fields.update(overrides)
    return OrderRequest(**fields)


def decode(tx: SignedTx):
    raw = cosmos_tx_pb2.TxRaw.FromString(tx.tx_bytes)
    body = cosmos_tx_pb2.TxBody.FromString(raw.body_bytes)
    auth = cosmos_tx_pb2.AuthInfo.FromString(raw.auth_info_bytes)
    assert len(body.messages) == 1
    exec_message = authz_tx_pb2.MsgExec()
    assert body.messages[0].Unpack(exec_message) or body.messages[0].type_url.endswith("MsgExec")
    exec_message = authz_tx_pb2.MsgExec.FromString(body.messages[0].value)
    return body, auth, exec_message


def inner_order(exec_message, index):
    return exchange_tx_pb2.MsgCreateDerivativeMarketOrder.FromString(exec_message.msgs[index].value)


# Trigger direction ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kind, side, order_type, above_mark",
    [
        (OrderKind.STOP, Side.SHORT, "STOP_SELL", False),
        (OrderKind.STOP, Side.LONG, "STOP_BUY", True),
        (OrderKind.TAKE, Side.SHORT, "TAKE_SELL", True),
        (OrderKind.TAKE, Side.LONG, "TAKE_BUY", False),
    ],
)
def test_conditional_type_and_direction_follow_chain_code(kind, side, order_type, above_mark):
    assert conditional_order_type(kind, side) == order_type
    assert trigger_is_above_mark(kind, side) is above_mark


@pytest.mark.parametrize(
    "buying, trigger, expected",
    [
        (False, "98", OrderKind.STOP),
        (False, "103", OrderKind.TAKE),
        (True, "103", OrderKind.STOP),
        (True, "98", OrderKind.TAKE),
    ],
)
def test_classify_conditional_infers_stop_or_take(buying, trigger, expected):
    assert classify_conditional(buying, Decimal(trigger), Decimal("100")) is expected


# Rounding ------------------------------------------------------------------------------------

def test_rounding_never_increases_risk():
    tick = Decimal("1")
    assert round_worst_price(Decimal("100.7"), tick, buying=True) == Decimal("100")
    assert round_worst_price(Decimal("100.2"), tick, buying=False) == Decimal("101")
    # Distance to the trigger shrinks for all four conditional types.
    assert round_trigger(Decimal("98.2"), tick, OrderKind.STOP, Side.SHORT) == Decimal("99")
    assert round_trigger(Decimal("102.7"), tick, OrderKind.STOP, Side.LONG) == Decimal("102")
    assert round_trigger(Decimal("102.7"), tick, OrderKind.TAKE, Side.SHORT) == Decimal("102")
    assert round_trigger(Decimal("98.2"), tick, OrderKind.TAKE, Side.LONG) == Decimal("99")


async def test_quantity_rounds_down_and_prices_land_on_ticks():
    backend = make()
    tx = await backend.build_and_sign(
        GRANTER, SUBACCOUNT,
        [market_order(quantity=Decimal("0.01239"), worst_price=Decimal("100.9"))],
    )
    _, _, exec_message = decode(tx)
    order = inner_order(exec_message, 0).order
    assert Decimal(order.order_info.quantity) / E18 == Decimal("0.0123")
    assert Decimal(order.order_info.price) / E18 == Decimal("100")


async def test_quantity_that_rounds_to_zero_is_refused():
    with pytest.raises(ValueError):
        await make().build_and_sign(GRANTER, SUBACCOUNT, [market_order(quantity=Decimal("0.00001"))])


# Message construction ------------------------------------------------------------------------

async def test_every_order_is_in_one_msgexec_signed_by_the_grantee():
    backend = make()
    tx = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order(), stop_order()])
    body, auth, exec_message = decode(tx)

    assert body.messages[0].type_url == "/cosmos.authz.v1beta1.MsgExec"
    assert exec_message.grantee == backend.grantee_address
    assert tx.signer == backend.grantee_address
    for index in range(2):
        message = inner_order(exec_message, index)
        assert message.sender == GRANTER
        assert message.order.order_info.subaccount_id == SUBACCOUNT
        assert message.order.order_info.fee_recipient == GRANTER
        assert message.order.market_id == MARKET_ID


async def test_stop_is_reduce_only_conditional_market_order():
    tx = await make().build_and_sign(GRANTER, SUBACCOUNT, [stop_order()])
    _, _, exec_message = decode(tx)
    assert exec_message.msgs[0].type_url == "/injective.exchange.v2.MsgCreateDerivativeMarketOrder"
    order = inner_order(exec_message, 0).order
    assert order.order_type == order_pb2.OrderType.STOP_SELL
    assert Decimal(order.margin) == 0
    assert Decimal(order.trigger_price) / E18 == Decimal("98")


@pytest.mark.parametrize(
    "overrides",
    [
        {"reduce_only": False},
        {"margin": Decimal("1")},
        {"trigger_price": None},
        {"trigger_price": Decimal("0")},
    ],
)
async def test_vanilla_conditional_stop_is_refused(overrides):
    with pytest.raises(ValueError):
        await make().build_and_sign(GRANTER, SUBACCOUNT, [stop_order(**overrides)])


@pytest.mark.parametrize(
    "overrides",
    [
        {"reduce_only": True},
        {"reduce_only": False, "margin": Decimal(0)},
        {"trigger_price": Decimal("98")},
        {"cid": "x" * 37},
    ],
)
async def test_invalid_market_order_is_refused(overrides):
    with pytest.raises(ValueError):
        await make().build_and_sign(GRANTER, SUBACCOUNT, [market_order(**overrides)])


async def test_subaccount_of_another_owner_is_refused():
    other = PrivateKey.from_hex("33" * 32).to_public_key().to_address().get_subaccount_id(1)
    with pytest.raises(ValueError):
        await make().build_and_sign(GRANTER, other, [market_order()])


async def test_cancel_uses_conditional_mask_without_direction_and_goes_first():
    backend = make()
    resting = RestingOrder(
        order_hash="0x" + "cd" * 32, cid="old", kind=OrderKind.TAKE, side=Side.SHORT,
        quantity=Decimal("0.01"), trigger_price=Decimal("103"), worst_price=Decimal("100"), reduce_only=True,
    )
    tx = await backend.build_and_sign(GRANTER, SUBACCOUNT, [stop_order()], cancels=[resting])
    _, _, exec_message = decode(tx)
    assert exec_message.msgs[0].type_url == "/injective.exchange.v2.MsgCancelDerivativeOrder"
    cancel = exchange_tx_pb2.MsgCancelDerivativeOrder.FromString(exec_message.msgs[0].value)
    assert cancel.order_mask == CANCEL_CONDITIONAL_MARKET_MASK == 36
    assert cancel.order_hash == resting.order_hash
    assert exec_message.msgs[1].type_url == "/injective.exchange.v2.MsgCreateDerivativeMarketOrder"


def test_gas_covers_chain_fixed_gas_and_refuses_batch_update():
    assert GAS_MARKET_ORDER >= 190_000
    message = exchange_tx_pb2.MsgCreateDerivativeMarketOrder()
    assert gas_limit_for([message]) == GAS_ANTE + GAS_MSG_EXEC + GAS_MARKET_ORDER
    with pytest.raises(ValueError):
        gas_limit_for([exchange_tx_pb2.MsgBatchUpdateOrders()])


def test_batch_update_is_never_built():
    source = Path(sys.modules[InjectiveChain.__module__].__file__).read_text()
    assert "msg_batch_update" not in source
    assert "exchange_tx_pb2.MsgBatchUpdateOrders" not in source


# Sequence and timeout height -----------------------------------------------------------------

async def test_timeout_height_and_sequence_are_read_fresh_for_each_tx():
    client = FakeClient()
    backend = make(client)

    first = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    body, auth, _ = decode(first)
    assert body.timeout_height == 1000 + TIMEOUT_BLOCKS == first.timeout_height
    assert auth.signer_infos[0].sequence == 5 == first.sequence

    client.height = 1100
    client.sequence = 6
    second = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    body, auth, _ = decode(second)
    assert body.timeout_height == 1100 + TIMEOUT_BLOCKS
    assert auth.signer_infos[0].sequence == 6
    assert client.account_reads == 2


async def test_sdk_local_sequence_counter_is_not_used():
    client = FakeClient()
    client.get_sequence = lambda: pytest.fail("the SDK's local sequence counter must not be used")
    await make(client).build_and_sign(GRANTER, SUBACCOUNT, [market_order()])


async def test_second_tx_is_refused_while_the_first_can_still_land():
    client = FakeClient()
    backend = make(client)
    first = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])

    client.height = first.timeout_height + EXPIRY_MARGIN_BLOCKS
    with pytest.raises(InFlightTx):
        await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])

    # Allowed once the sequence has been consumed.
    client.sequence = first.sequence + 1
    await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])


async def test_new_tx_allowed_after_the_previous_expired():
    client = FakeClient()
    backend = make(client)
    first = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    client.height = first.timeout_height + EXPIRY_MARGIN_BLOCKS + 1
    second = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    assert second.sequence == first.sequence


async def test_unfunded_grantee_is_a_distinct_error():
    client = FakeClient()
    client.account_error = rpc_error(grpc.StatusCode.NOT_FOUND)
    with pytest.raises(GranteeAccountNotFound):
        await make(client).build_and_sign(GRANTER, SUBACCOUNT, [market_order()])


async def test_account_transport_failure_signs_nothing():
    client = FakeClient()
    client.account_error = rpc_error(grpc.StatusCode.UNAVAILABLE)
    backend = make(client)
    with pytest.raises(ChainUnavailable):
        await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    assert backend._last_signed is None


# Broadcast and lookup ------------------------------------------------------------------------

async def signed(client=None):
    client = client or FakeClient()
    backend = make(client)
    return backend, client, await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])


async def test_hash_is_sha256_of_the_exact_bytes():
    _, _, tx = await signed()
    assert tx.tx_hash == hashlib.sha256(tx.tx_bytes).hexdigest().upper()


async def test_broadcast_sends_the_same_bytes_every_time():
    backend, client, tx = await signed()
    await backend.broadcast(tx)
    await backend.broadcast(tx)
    assert client.broadcasts == [tx.tx_bytes, tx.tx_bytes]


async def test_broadcast_transport_error_raises_unavailable():
    backend, client, tx = await signed()
    client.broadcast_error = rpc_error(grpc.StatusCode.UNAVAILABLE)
    with pytest.raises(ChainUnavailable):
        await backend.broadcast(tx)


@pytest.mark.parametrize("code", [19, 32, 5])
async def test_checktx_codes_do_not_resend_or_raise(code):
    backend, client, tx = await signed()
    client.broadcast_result = {"txResponse": {"code": code}}
    await backend.broadcast(tx)
    assert client.broadcasts == [tx.tx_bytes]


async def test_lookup_included_ok_and_failed():
    backend, client, tx = await signed()
    result = await backend.lookup(tx)
    assert (result.status, result.height) == (TxStatus.OK, 1001)

    client.tx_response = {"code": 81, "height": "1002", "rawLog": "no margin locked"}
    result = await backend.lookup(tx)
    assert (result.status, result.code) == (TxStatus.FAILED, 81)
    assert client.fetched_hashes[-1] == tx.tx_hash


async def test_lookup_not_found_is_pending_until_timeout_margin_has_passed():
    backend, client, tx = await signed()
    client.tx_error = rpc_error(grpc.StatusCode.NOT_FOUND)

    client.height = tx.timeout_height + EXPIRY_MARGIN_BLOCKS
    assert (await backend.lookup(tx)).status is TxStatus.PENDING

    client.height = tx.timeout_height + EXPIRY_MARGIN_BLOCKS + 1
    assert (await backend.lookup(tx)).status is TxStatus.EXPIRED


async def test_lookup_transport_error_is_unavailable_never_expired():
    backend, client, tx = await signed()
    client.tx_error = rpc_error(grpc.StatusCode.UNAVAILABLE)
    client.height = tx.timeout_height + 100
    with pytest.raises(ChainUnavailable):
        await backend.lookup(tx)


def test_indexer_client_is_not_imported():
    source = Path(sys.modules[InjectiveChain.__module__].__file__).read_text()
    assert "indexer_client" not in source


def test_sdk_github_token_list_is_never_loaded():
    source = Path(sys.modules[InjectiveChain.__module__].__file__).read_text()
    for name in ("all_derivative_markets", "all_tokens", "initialize_tokens"):
        assert name not in source


# Mainnet gate --------------------------------------------------------------------------------

async def test_mainnet_refuses_signing_without_the_flag():
    backend = make(network="mainnet", allow_mainnet=False)
    with pytest.raises(MainnetRefused):
        await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    assert backend._client.account_reads == 0


async def test_mainnet_refuses_broadcast_without_the_flag():
    backend = make(network="mainnet", allow_mainnet=False)
    tx = SignedTx(tx_hash="AA", tx_bytes=b"x", signer="inj1", sequence=1, timeout_height=2)
    with pytest.raises(MainnetRefused):
        await backend.broadcast(tx)
    assert backend._client.broadcasts == []


async def test_mainnet_signs_only_with_the_flag():
    backend = make(network="mainnet", allow_mainnet=True)
    tx = await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])
    assert tx.tx_bytes


def test_unknown_network_is_refused():
    with pytest.raises(ValueError):
        make(network="devnet")


# Reads ---------------------------------------------------------------------------------------

async def test_market_metadata_is_converted_from_chain_format():
    info = await make().market()
    assert info.market_id == MARKET_ID
    assert info.price_tick == Decimal("1")
    assert info.quantity_tick == Decimal("0.0001")
    assert info.min_notional == Decimal("1")
    assert info.taker_fee == Decimal("0.0005")
    assert info.initial_margin_ratio == Decimal("0.02")


async def test_snapshot_reads_mark_oracle_book_and_depth_within_collar():
    snapshot = await make().snapshot()
    assert snapshot.mark_price == Decimal("100")
    assert snapshot.oracle_price == Decimal("100.5")
    assert snapshot.best_bid == Decimal("99.9")
    assert snapshot.best_ask == Decimal("100.1")
    assert snapshot.bid_depth_usd == Decimal("99.9") * 2  # The 90 level is outside the collar.
    assert snapshot.ask_depth_usd == Decimal("100.1") * 3
    assert snapshot.height == 1000


async def test_provider_oracle_uses_mark_because_the_chain_query_refuses_it():
    client = FakeClient()
    market = raw_market()
    market["market"]["oracleType"] = "Provider"

    async def provider_markets(status=None, market_ids=None, with_mid_price_and_tob=None):
        return {"markets": [market]}

    async def provider_market(market_id):
        return {"market": market}

    client.fetch_chain_derivative_markets = provider_markets
    client.fetch_chain_derivative_market = provider_market
    backend = make(client)
    backend._oracle_stub = None  # A Provider market must not reach the oracle query.
    assert (await backend.snapshot()).oracle_price == Decimal("100")


async def test_oracle_failure_falls_back_to_mark_and_reads_as_stale():
    backend = make()

    class Failing:
        async def OraclePrice(self, request):
            raise rpc_error(grpc.StatusCode.UNAVAILABLE)

    backend._oracle_stub = Failing()
    snapshot = await backend.snapshot()
    assert snapshot.oracle_price == snapshot.mark_price == Decimal("100")
    assert snapshot.as_of_ms == 0  # entries see it as stale, reads that only need the mark do not


async def test_snapshot_with_empty_side_reports_zero():
    client = FakeClient()
    client.book = {"buysPriceLevel": [], "sellsPriceLevel": []}
    snapshot = await make(client).snapshot()
    assert (snapshot.best_bid, snapshot.best_ask, snapshot.bid_depth_usd) == (0, 0, 0)


def test_funding_rate_matches_chain_formula_and_cap():
    info = raw_market()["perpetualInfo"]["marketInfo"]
    funding = dict(raw_market()["perpetualInfo"]["fundingInfo"])
    assert predicted_funding_rate(info, funding) == Decimal("0.00000416666")

    # 1800 s into the interval: lastTimestamp + 3600 - nextFundingTimestamp.
    funding.update(cumulativePrice=chain_value("0.72"), lastTimestamp=str(1794153600 - 3600 + 1800))
    expected = Decimal("0.72") / (1800 * 24) + Decimal("0.00000416666")
    assert predicted_funding_rate(info, funding) == expected

    funding.update(cumulativePrice=chain_value("1000"))
    assert predicted_funding_rate(info, funding) == Decimal("0.000625")
    funding.update(cumulativePrice=chain_value("-1000"))
    assert predicted_funding_rate(info, funding) == Decimal("-0.000625")


async def test_account_flat():
    account = await make().account(GRANTER, SUBACCOUNT)
    assert account.position is None
    assert account.equity == Decimal("100")
    assert account.available == Decimal("80")
    assert account.orders == ()
    assert account.height == 1000


async def test_equity_adds_position_margin_back_and_counts_funding():
    client = FakeClient()
    client.position_state = {
        "isLong": True,
        "quantity": chain_value("2"),
        "entryPrice": chain_value("90"),
        "margin": chain_value("50"),
        "cumulativeFundingEntry": chain_value("9"),
    }
    account = await make(client).account(GRANTER, SUBACCOUNT)
    # Price pnl (100 - 90) * 2 = 20, funding paid (10 - 9) * 2 = 2.
    assert account.position.unrealized_pnl == Decimal("18")
    assert account.equity == Decimal("100") + Decimal("50") + Decimal("18")


async def test_short_position_omitting_is_long_and_receiving_funding():
    client = FakeClient()
    client.position_state = {
        "quantity": chain_value("2"),
        "entryPrice": chain_value("110"),
        "margin": chain_value("50"),
        "cumulativeFundingEntry": chain_value("9"),
    }
    account = await make(client).account(GRANTER, SUBACCOUNT)
    assert account.position.side is Side.SHORT
    assert account.position.unrealized_pnl == Decimal("22")


async def test_conditional_orders_are_parsed_and_limit_ones_skipped():
    client = FakeClient()
    client.conditional_orders = [
        {"isBuy": False, "isLimit": False, "price": chain_value("95"), "quantity": chain_value("1"),
         "margin": "0", "triggerPrice": chain_value("98"), "orderHash": "0xstop", "cid": "s"},
        {"isBuy": False, "isLimit": False, "price": chain_value("101"), "quantity": chain_value("1"),
         "margin": "0", "triggerPrice": chain_value("104"), "orderHash": "0xtake", "cid": "t"},
        {"isBuy": True, "isLimit": True, "price": chain_value("1"), "quantity": chain_value("1"),
         "margin": chain_value("1"), "triggerPrice": chain_value("120"), "orderHash": "0xother"},
    ]
    orders = (await make(client).account(GRANTER, SUBACCOUNT)).orders
    assert [order.kind for order in orders] == [OrderKind.STOP, OrderKind.TAKE]
    assert orders[0].side is Side.SHORT
    assert orders[0].reduce_only is True


# Grants --------------------------------------------------------------------------------------

NOW_MS = 1_800_000_000_000
LATER_MS = NOW_MS + 3_600_000


def required_records(**overrides):
    fields = dict(subaccount_id=SUBACCOUNT, market_ids=(MARKET_ID,), expires_ms=LATER_MS)
    fields.update(overrides)
    return [grants.GrantRecord(authz_type_url=spec.authz_type_url, **fields) for spec in grants.REQUIRED_GRANTS]


def verify(records):
    return grants.verify_records(records, SUBACCOUNT, MARKET_ID, NOW_MS)


def test_grant_list_is_exactly_two_exchange_v2_types():
    assert [spec.authz_type_url for spec in grants.REQUIRED_GRANTS] == [
        "/injective.exchange.v2.CreateDerivativeMarketOrderAuthz",
        "/injective.exchange.v2.CancelDerivativeOrderAuthz",
    ]


def test_grant_list_has_no_transfer_withdraw_or_batch_update():
    joined = " ".join(spec.msg_type_url for spec in grants.REQUIRED_GRANTS)
    for forbidden in ("Send", "Withdraw", "Transfer", "Batch"):
        assert forbidden not in joined


def test_docs_list_every_required_type():
    text = (Path(__file__).resolve().parent.parent / "docs" / "GRANTS.md").read_text()
    for spec in grants.REQUIRED_GRANTS:
        assert spec.authz_type_url in text
        assert spec.msg_type_url in text


def test_built_messages_decode_to_the_required_list():
    messages = grants.build_grant_msgs(GRANTER, "inj1grantee", SUBACCOUNT, MARKET_ID, expires_unix_s=1_900_000_000)
    assert len(messages) == len(grants.REQUIRED_GRANTS)
    records = [grants.decode_grant(message.grant) for message in messages]
    assert [record.authz_type_url for record in records] == [spec.authz_type_url for spec in grants.REQUIRED_GRANTS]
    assert all(record.subaccount_id == SUBACCOUNT and record.market_ids == (MARKET_ID,) for record in records)
    assert all(record.expires_ms == 1_900_000_000_000 for record in records)
    assert all(message.granter == GRANTER and message.grantee == "inj1grantee" for message in messages)


def test_verify_accepts_exact_grants_and_reports_earliest_expiry():
    records = required_records()
    records[1] = grants.GrantRecord(records[1].authz_type_url, SUBACCOUNT, (MARKET_ID,), NOW_MS + 1000)
    status = verify(records)
    assert status.ok
    assert status.missing == ()
    assert status.expires_ms == NOW_MS + 1000


def test_verify_reports_each_missing_message():
    status = verify(required_records()[:1])
    assert not status.ok
    assert status.missing == (grants.REQUIRED_GRANTS[1].msg_type_url,)


def test_verify_rejects_expired():
    assert not verify(required_records(expires_ms=NOW_MS)).ok


def test_verify_rejects_grant_with_no_expiry():
    status = verify(required_records(expires_ms=None))
    assert not status.ok
    assert status.expires_ms is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"subaccount_id": "0x" + "cc" * 32},
        {"subaccount_id": SUBACCOUNT.upper()},
        {"market_ids": ("0x" + "dd" * 32,)},
        {"market_ids": (MARKET_ID, "0x" + "dd" * 32)},
        {"market_ids": ()},
    ],
)
def test_verify_rejects_wrong_scope(overrides):
    assert not verify(required_records(**overrides)).ok


@pytest.mark.parametrize(
    "extra_type",
    [
        "/cosmos.authz.v1beta1.GenericAuthorization",
        "/cosmos.bank.v1beta1.SendAuthorization",
        "/injective.exchange.v2.BatchUpdateOrdersAuthz",
        "/injective.exchange.v2.CreateDerivativeLimitOrderAuthz",
        "/injective.exchange.v1beta1.CreateDerivativeMarketOrderAuthz",
    ],
)
def test_verify_fails_closed_on_any_extra_grant(extra_type):
    status = verify(required_records() + [grants.GrantRecord(authz_type_url=extra_type)])
    assert not status.ok
    assert status.missing == (grants.UNEXPECTED_GRANT,)
    assert extra_type not in status.missing  # Chain strings never reach agent-facing state.


def test_decode_handles_unknown_authorization_without_error():
    authorization = any_pb2.Any(type_url="/some.future.v9.UnknownAuthz", value=b"\x01")
    grant = authz_pb2.Grant(authorization=authorization, expiration=timestamp_pb2.Timestamp(seconds=5))
    record = grants.decode_grant(grant)
    assert record.authz_type_url == "/some.future.v9.UnknownAuthz"
    assert record.expires_ms == 5000


async def test_check_grant_reads_every_page_from_chain():
    client = FakeClient()
    messages = grants.build_grant_msgs(GRANTER, "inj1g", SUBACCOUNT, MARKET_ID, expires_unix_s=1_900_000_000)
    client.stubAuthz.pages = [
        SimpleNamespace(grants=[messages[0].grant], pagination=SimpleNamespace(next_key=b"more")),
        SimpleNamespace(grants=[messages[1].grant], pagination=SimpleNamespace(next_key=b"")),
    ]
    assert (await make(client, now_ms=NOW_MS).check_grant(GRANTER, SUBACCOUNT)).ok


async def test_check_grant_with_nothing_granted():
    status = await make().check_grant(GRANTER, SUBACCOUNT)
    assert not status.ok
    assert len(status.missing) == len(grants.REQUIRED_GRANTS)


async def test_check_grant_transport_failure_is_unavailable():
    client = FakeClient()

    async def failing(request, metadata=None):
        raise rpc_error(grpc.StatusCode.UNAVAILABLE)

    client.stubAuthz.Grants = failing
    with pytest.raises(ChainUnavailable):
        await make(client).check_grant(GRANTER, SUBACCOUNT)


# Factory and key file ------------------------------------------------------------------------

def settings(**overrides):
    fields = dict(
        chain_backend="injective", network="testnet", candle_network="", candle_source="", market_ticker="BTC/USDC PERP",
        grantee_key_path="", tx_timeout_blocks=40, allow_mainnet=False,
        risk=SimpleNamespace(collar=Decimal("0.005")),
        chain_rps=15,
        indexer_rps=30,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def write_key(tmp_path, mode=0o600, value="0x" + GRANTEE_KEY):
    path = tmp_path / "key"
    path.write_text(value)
    os.chmod(path, mode)
    return str(path)


def test_factory_builds_injective_backend(tmp_path):
    backend = make_chain(settings(grantee_key_path=write_key(tmp_path)))
    assert isinstance(backend, InjectiveChain)
    assert backend.is_mainnet is False


async def test_factory_passes_allow_mainnet_through(tmp_path):
    backend = make_chain(settings(network="mainnet", grantee_key_path=write_key(tmp_path), allow_mainnet=False))
    assert isinstance(backend, InjectiveChain) and backend.is_mainnet
    with pytest.raises(MainnetRefused):
        await backend.build_and_sign(GRANTER, SUBACCOUNT, [market_order()])


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError):
        make_chain(settings(chain_backend="other"))


def test_factory_returns_mock():
    pytest.importorskip("halo_trade.chain.mock")
    from halo_trade.chain.mock import MockChain

    assert isinstance(make_chain(settings(chain_backend="mock")), MockChain)


def test_key_file_must_be_private(tmp_path):
    with pytest.raises(PermissionError):
        load_grantee_key(write_key(tmp_path, mode=0o644))


def test_key_file_strips_prefix_and_whitespace(tmp_path):
    assert load_grantee_key(write_key(tmp_path, value=f"0x{GRANTEE_KEY}\n")) == GRANTEE_KEY


def test_missing_key_path_is_refused():
    with pytest.raises(ValueError):
        load_grantee_key("")


# Scripts -------------------------------------------------------------------------------------

def load_script(name):
    path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("network", ["mainnet", "devnet", ""])
def test_setup_script_refuses_anything_but_testnet(network):
    module = load_script("testnet_setup")
    with pytest.raises(SystemExit):
        module.require_testnet(network)


def test_setup_script_allows_testnet():
    load_script("testnet_setup").require_testnet("testnet")


@pytest.mark.parametrize("script", ["testnet_setup", "testnet_cases"])
def test_scripts_refuse_mainnet_from_the_environment(script, monkeypatch):
    module = load_script(script)
    monkeypatch.setenv("HALO_NETWORK", "mainnet")
    monkeypatch.setattr(sys, "argv", [script])
    with pytest.raises(SystemExit) as refused:
        module.main()
    assert "testnet only" in str(refused.value)


async def test_setup_script_refuses_a_node_reporting_another_chain():
    module = load_script("testnet_setup")
    client = FakeClient()

    async def mainnet_block():
        return {"block": {"header": {"height": "5", "chainId": "injective-1"}}}

    client.fetch_latest_block = mainnet_block
    with pytest.raises(SystemExit):
        await module.require_testnet_chain(make(client))


async def test_funding_check_counts_quote_already_deposited():
    module = load_script("testnet_setup")
    client = FakeClient()

    async def bank(address, denom):
        return {"balance": {"amount": str(10**18) if denom == "inj" else "0"}}

    client.fetch_bank_balance = bank
    client.deposits = {"availableBalance": chain_value("400"), "totalBalance": chain_value("400")}
    keys = {"grantee": "44" * 32, "trader": "55" * 32, "maker": "66" * 32}
    assert await module.funding_shortfalls(make(client), keys, QUOTE, 6) == []

    client.deposits = {"availableBalance": "0", "totalBalance": "0"}
    short = await module.funding_shortfalls(make(client), keys, QUOTE, 6)
    assert len(short) == 2 and all(QUOTE in line for line in short)


# The test-only limit-order grant ------------------------------------------------------------------

TESTNET_KEYS = {"grantee": GRANTEE_KEY, "trader": "55" * 32, "maker": "66" * 32}
FAR_FUTURE_S = 1_900_000_000


def grant_page(*grant_messages):
    return SimpleNamespace(grants=[message.grant for message in grant_messages], pagination=SimpleNamespace(next_key=b""))


def trader_chain(*grant_messages, now_ms=NOW_MS):
    client = FakeClient()
    client.stubAuthz.pages = [grant_page(*grant_messages)]
    return make(client, now_ms=now_ms)


def test_setup_issues_the_production_list_and_only_the_named_extra():
    module = load_script("testnet_setup")
    messages = module.grant_messages(TESTNET_KEYS, MARKET_ID, FAR_FUTURE_S)
    urls = [message.grant.authorization.type_url for message in messages]
    assert urls == [
        *(spec.authz_type_url for spec in grants.REQUIRED_GRANTS),
        module.TESTNET_ONLY_LIMIT_ORDER_GRANT.authz_type_url,
    ]
    assert urls[-1] == "/injective.exchange.v2.CreateDerivativeLimitOrderAuthz"
    assert module.TESTNET_ONLY_LIMIT_ORDER_GRANT not in grants.REQUIRED_GRANTS

    # Scoped like the others: the test wallet's subaccount, the one market, an expiry.
    extra = module.TESTNET_ONLY_LIMIT_ORDER_GRANT.authz_class()
    extra.ParseFromString(messages[-1].grant.authorization.value)
    assert extra.subaccount_id == module.subaccount_of(TESTNET_KEYS["trader"])
    assert list(extra.market_ids) == [MARKET_ID]
    assert messages[-1].grant.expiration.seconds == FAR_FUTURE_S


async def test_cases_gate_accepts_the_named_extra_that_check_grant_refuses():
    module = load_script("testnet_setup")
    messages = module.grant_messages(TESTNET_KEYS, MARKET_ID, FAR_FUTURE_S)
    assert await module.grant_problems(trader_chain(*messages), TESTNET_KEYS, MARKET_ID) == []

    # The same grants fail the production check, which is what keeps this wallet out of the service.
    status = await trader_chain(*messages).check_grant(module.address_of(TESTNET_KEYS["trader"]), module.subaccount_of(TESTNET_KEYS["trader"]))
    assert not status.ok and status.missing == (grants.UNEXPECTED_GRANT,)


async def test_cases_gate_blocks_without_the_extra_or_with_it_expired():
    module = load_script("testnet_setup")
    messages = module.grant_messages(TESTNET_KEYS, MARKET_ID, FAR_FUTURE_S)
    extra_url = module.TESTNET_ONLY_LIMIT_ORDER_GRANT.msg_type_url
    assert await module.grant_problems(trader_chain(*messages[:-1]), TESTNET_KEYS, MARKET_ID) == [extra_url]

    expired = module.grant_messages(TESTNET_KEYS, MARKET_ID, FAR_FUTURE_S)
    expired[-1].grant.expiration.seconds = NOW_MS // 1000 - 1
    assert await module.grant_problems(trader_chain(*expired), TESTNET_KEYS, MARKET_ID) == [extra_url]


async def test_cases_gate_still_fails_on_any_other_stray_grant_or_missing_production_grant():
    module = load_script("testnet_setup")
    messages = module.grant_messages(TESTNET_KEYS, MARKET_ID, FAR_FUTURE_S)
    stray = authz_pb2.Grant(
        authorization=any_pb2.Any(type_url="/cosmos.bank.v1beta1.SendAuthorization", value=b""),
        expiration=timestamp_pb2.Timestamp(seconds=FAR_FUTURE_S),
    )
    with_stray = trader_chain(*messages, SimpleNamespace(grant=stray))
    assert await module.grant_problems(with_stray, TESTNET_KEYS, MARKET_ID) == [grants.UNEXPECTED_GRANT]

    assert await module.grant_problems(trader_chain(*messages[1:]), TESTNET_KEYS, MARKET_ID) == [grants.REQUIRED_GRANTS[0].msg_type_url]


async def test_issuing_the_extra_refuses_a_node_on_another_chain():
    module = load_script("testnet_setup")
    client = FakeClient()

    async def mainnet_block():
        return {"block": {"header": {"height": "5", "chainId": "injective-1"}}}

    client.fetch_latest_block = mainnet_block
    with pytest.raises(SystemExit):
        await module.issue_grant(make(client), TESTNET_KEYS, MARKET_ID)


def test_wallet_file_is_private_and_stable(tmp_path):
    module = load_script("testnet_setup")
    path = tmp_path / "data" / "wallets.json"
    first = module.load_or_create_wallets(path)
    assert set(first) == {"grantee", "trader", "maker"}
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert module.load_or_create_wallets(path) == first


def test_wallet_file_permissions_are_repaired(tmp_path):
    module = load_script("testnet_setup")
    path = tmp_path / "wallets.json"
    module.load_or_create_wallets(path)
    os.chmod(path, 0o644)
    module.load_or_create_wallets(path)
    assert os.stat(path).st_mode & 0o777 == 0o600
