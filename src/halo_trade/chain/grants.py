"""Trade-only AuthZ grants: the exact list, and the on-chain check.

The server's grantee key may do two things for one subaccount in one market: place a market
or conditional order, and cancel an order. There is deliberately no send, withdraw,
subaccount-transfer or batch-update permission here, and no limit-order permission: the service
never places limit orders. `scripts/testnet_setup.py` issues that one as a named, test-only extra.

`docs/GRANTS.md` states the same list for the TypeScript onboarding page. Keep them in step.

Why these types: `CreateDerivativeMarketOrderAuthz.Accept` checks only the subaccount and the
market (injective-core release/v1.20.x, types/v2/authz.go), so a conditional STOP/TAKE sent as
MsgCreateDerivativeMarketOrder falls under the market grant. `scripts/testnet_cases.py`
confirms that on chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import grpc
from google.protobuf import any_pb2, timestamp_pb2
from pyinjective.proto.cosmos.authz.v1beta1 import (
    authz_pb2,
    query_pb2 as authz_query_pb2,
    tx_pb2 as authz_tx_pb2,
)
from pyinjective.proto.cosmos.base.query.v1beta1 import pagination_pb2
from pyinjective.proto.injective.exchange.v2 import authz_pb2 as exchange_authz_pb2

from .base import ChainUnavailable, GrantStatus

MSG_GRANT_TYPE_URL = "/cosmos.authz.v1beta1.MsgGrant"
GENERIC_AUTHZ_TYPE_URL = "/cosmos.authz.v1beta1.GenericAuthorization"

# Our own label for "the grantee holds something beyond the list". It goes into
# GrantStatus.missing in place of the chain's string, which must not reach agent-facing state.
UNEXPECTED_GRANT = "unexpected_grant"

# A web-issued grant lasts this long, then the user renews it on the same page.
DEFAULT_GRANT_TTL_S = 30 * 24 * 3600


@dataclass(frozen=True)
class GrantSpec:
    authz_type_url: str
    msg_type_url: str
    authz_class: type


REQUIRED_GRANTS: tuple[GrantSpec, ...] = (
    GrantSpec(
        authz_type_url="/injective.exchange.v2.CreateDerivativeMarketOrderAuthz",
        msg_type_url="/injective.exchange.v2.MsgCreateDerivativeMarketOrder",
        authz_class=exchange_authz_pb2.CreateDerivativeMarketOrderAuthz,
    ),
    GrantSpec(
        authz_type_url="/injective.exchange.v2.CancelDerivativeOrderAuthz",
        msg_type_url="/injective.exchange.v2.MsgCancelDerivativeOrder",
        authz_class=exchange_authz_pb2.CancelDerivativeOrderAuthz,
    ),
)

_SPEC_BY_AUTHZ = {spec.authz_type_url: spec for spec in REQUIRED_GRANTS}


@dataclass(frozen=True)
class GrantRecord:
    """One grant as stored on chain, decoded only as far as the check needs."""

    authz_type_url: str
    subaccount_id: str = ""
    market_ids: tuple[str, ...] = ()
    expires_ms: Optional[int] = None


def build_grant_msg(
    spec: GrantSpec,
    granter: str,
    grantee: str,
    subaccount_id: str,
    market_id: str,
    expires_unix_s: int,
) -> authz_tx_pb2.MsgGrant:
    """One MsgGrant, scoped to one subaccount and one market."""
    authorization = any_pb2.Any()
    authorization.Pack(
        spec.authz_class(subaccount_id=subaccount_id, market_ids=[market_id]),
        type_url_prefix="",
    )
    return authz_tx_pb2.MsgGrant(
        granter=granter,
        grantee=grantee,
        grant=authz_pb2.Grant(
            authorization=authorization,
            expiration=timestamp_pb2.Timestamp(seconds=expires_unix_s),
        ),
    )


def build_grant_msgs(
    granter: str,
    grantee: str,
    subaccount_id: str,
    market_id: str,
    expires_unix_s: int,
) -> list[authz_tx_pb2.MsgGrant]:
    """The MsgGrant list the granter signs. One message per required authorization."""
    return [
        build_grant_msg(spec, granter, grantee, subaccount_id, market_id, expires_unix_s)
        for spec in REQUIRED_GRANTS
    ]


def decode_grant(grant: authz_pb2.Grant) -> GrantRecord:
    """Decode by type URL so an authorization the SDK has no class for cannot break the check."""
    type_url = grant.authorization.type_url
    expires_ms = None
    if grant.HasField("expiration"):
        expires_ms = grant.expiration.seconds * 1000 + grant.expiration.nanos // 1_000_000

    spec = _SPEC_BY_AUTHZ.get(type_url)
    if spec is None:
        return GrantRecord(authz_type_url=type_url, expires_ms=expires_ms)

    authorization = spec.authz_class()
    authorization.ParseFromString(grant.authorization.value)
    return GrantRecord(
        authz_type_url=type_url,
        subaccount_id=authorization.subaccount_id,
        market_ids=tuple(authorization.market_ids),
        expires_ms=expires_ms,
    )


def verify_records(
    records: Iterable[GrantRecord],
    subaccount_id: str,
    market_id: str,
    now_ms: int,
) -> GrantStatus:
    """Pure check of the granter-to-grantee grant set against the required list.

    Fails closed on any of: a required grant absent, expired or with no expiry, scoped to another
    subaccount, or scoped to anything but exactly this one market; or a grant outside the list, which includes
    a GenericAuthorization for one of our message types (it would not be subaccount scoped).

    Subaccount and market are compared as exact strings because that is what `Accept` does.
    """
    records = list(records)
    by_type = {record.authz_type_url: record for record in records}
    missing: list[str] = []
    earliest: Optional[int] = None

    for spec in REQUIRED_GRANTS:
        record = by_type.get(spec.authz_type_url)
        if (
            record is None
            or record.subaccount_id != subaccount_id
            or record.market_ids != (market_id,)
            or record.expires_ms is None
            or record.expires_ms <= now_ms
        ):
            missing.append(spec.msg_type_url)
            continue
        earliest = record.expires_ms if earliest is None else min(earliest, record.expires_ms)

    if any(record.authz_type_url not in _SPEC_BY_AUTHZ for record in records):
        missing.append(UNEXPECTED_GRANT)

    return GrantStatus(ok=not missing, missing=tuple(missing), expires_ms=earliest if not missing else None)


async def fetch_grant_records(
    stub,
    granter: str,
    grantee: str,
    metadata: Optional[Sequence] = None,
) -> list[GrantRecord]:
    """Read every grant from granter to grantee through chain gRPC, following pagination."""
    records: list[GrantRecord] = []
    next_key = b""
    while True:
        request = authz_query_pb2.QueryGrantsRequest(
            granter=granter,
            grantee=grantee,
            pagination=pagination_pb2.PageRequest(key=next_key, limit=100),
        )
        try:
            response = await stub.Grants(request, metadata=metadata)
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return records
            raise ChainUnavailable("authz grants query failed") from exc
        records.extend(decode_grant(grant) for grant in response.grants)
        next_key = response.pagination.next_key
        if not next_key:
            return records


async def verify_grant_on_chain(
    stub,
    granter: str,
    grantee: str,
    subaccount_id: str,
    market_id: str,
    now_ms: int,
    metadata: Optional[Sequence] = None,
) -> GrantStatus:
    records = await fetch_grant_records(stub, granter, grantee, metadata=metadata)
    return verify_records(records, subaccount_id=subaccount_id, market_id=market_id, now_ms=now_ms)
