# Trade-only AuthZ grants

The onboarding page builds exactly this list. The Python source of truth is
`src/halo_trade/chain/grants.py`; `check_grant` rejects anything that differs.

## What the user signs

One transaction from the user's wallet (the granter), containing **two** messages, one per row.
Each is a `/cosmos.authz.v1beta1.MsgGrant`.

| # | `grant.authorization` type URL | Permits this message |
|---|---|---|
| 1 | `/injective.exchange.v2.CreateDerivativeMarketOrderAuthz` | `/injective.exchange.v2.MsgCreateDerivativeMarketOrder` (market entries, closes, and conditional STOP/TAKE orders) |
| 2 | `/injective.exchange.v2.CancelDerivativeOrderAuthz` | `/injective.exchange.v2.MsgCancelDerivativeOrder` |

The `v2` package matters. A `v1beta1` authorization covers a different message type and fails the check.

## Fields

`MsgGrant`:

| Field | Value |
|---|---|
| `granter` | The user's `inj1...` address |
| `grantee` | The server's grantee `inj1...` address (from server config, never from the client) |
| `grant.authorization` | `Any` with the type URL from the table and the body below |
| `grant.expiration` | RFC 3339 timestamp, now + 30 days (`DEFAULT_GRANT_TTL_S`). Required and must be in the future: `check_grant` treats a grant with no expiry as missing |

Authorization body, the same for both:

| Field | Value |
|---|---|
| `subaccount_id` | The user's dedicated subaccount, `0x` + 64 lowercase hex characters |
| `market_ids` | An array with exactly one entry, the configured market ID, `0x` + 64 lowercase hex characters |

Example for row 1 as proto JSON:

```json
{
  "@type": "/cosmos.authz.v1beta1.MsgGrant",
  "granter": "inj1user...",
  "grantee": "inj1server...",
  "grant": {
    "authorization": {
      "@type": "/injective.exchange.v2.CreateDerivativeMarketOrderAuthz",
      "subaccount_id": "0x<40 hex of the user's address><24 hex subaccount nonce>",
      "market_ids": ["0x<64 hex market id>"]
    },
    "expiration": "2026-12-09T00:00:00Z"
  }
}
```

The subaccount ID is the user's 20-byte address in hex followed by a 12-byte big-endian nonce. Use a
non-zero nonce (nonce 1 is the dedicated trading subaccount), because the zero subaccount is backed
by the user's bank balance.

## What must not be granted

The check fails if the grantee holds **any** other grant from this user, so do not add:

- `/cosmos.bank.v1beta1.MsgSend`, or any `/injective.exchange.*.MsgWithdraw`, `MsgSubaccountTransfer`, `MsgExternalTransfer`
- `/injective.exchange.*.MsgBatchUpdateOrders` or `BatchUpdateOrdersAuthz`
- `/injective.exchange.v2.CreateDerivativeLimitOrderAuthz`: the service never places limit orders.
  Only `scripts/testnet_setup.py` issues it, on testnet, as a named test-only extra (see below).
- `/cosmos.authz.v1beta1.GenericAuthorization` for any message, including the two above. A generic
  grant is not limited to one subaccount or market.

## How the server verifies

`check_grant` reads all grants from granter to grantee through chain gRPC (`cosmos.authz.v1beta1.Query/Grants`).
It returns `ok=True` only if all of these hold:

- both grants are present
- `subaccount_id` is exactly the user's subaccount, and `market_ids` is exactly the one market
- each has an expiry and none has expired
- no other grant exists between the two addresses

`GrantStatus.missing` lists the message type URLs that failed, plus `unexpected_grant` if something
extra exists. `expires_ms` is the earlier expiry of the two, for a renewal reminder.

## The test-only extra

`scripts/testnet_cases.py` rests limit orders through the grant (cases b, c and d), so
`scripts/testnet_setup.py` gives its trader wallet one extra authorization on testnet only,
`CreateDerivativeLimitOrderAuthz`, named `TESTNET_ONLY_LIMIT_ORDER_GRANT`. It is not in
`REQUIRED_GRANTS` and the page's compiled list refuses it. Because of it, `check_grant` reports that
wallet as `unexpected_grant`, so the scripts use their own check (`grant_problems`) that accepts
only this extra and still fails on anything else. The trader wallet therefore cannot be onboarded
through the service; use a fresh wallet for that. The maker wallet signs its own house-liquidity
orders and needs no grant.

## Not yet verified

No grant has been issued on any network yet, because the test wallets could not be funded. Two
things need checking when the page is built:

- **Whether a wallet can sign this through EIP-712.** MetaMask users sign Cosmos messages as EIP-712
  typed data, built from amino JSON. The v2 authorizations declare amino names such as
  `exchange/CreateDerivativeMarketOrderAuthz`. Confirm that `@injectivelabs/sdk-ts` can encode these
  typed authorizations inside `MsgGrant`, since its documented helpers are for generic grants.
- **Whether the market grant really covers conditional orders on the live chain.** The last public
  chain code (`release/v1.20.x`) says it does, because `Accept` checks only subaccount and market.
  `scripts/testnet_cases.py` tests this directly.

After the grant, the user's wallet also needs to deposit into the subaccount. A grant moves no funds.
