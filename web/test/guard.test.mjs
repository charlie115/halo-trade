// The checks that stand between a tampered server response and the wallet. Ported from the
// security review's page_hostile.mjs: there these calls went straight through; here they must not.
import assert from "node:assert/strict";
import { test } from "node:test";
import { getInjectiveAddress } from "@injectivelabs/sdk-ts";
import { ALLOWED_GRANTS, MAX_GRANT_TTL_S, buildGrantMsgs, buildRevokeMsgs, grantExpiryUnix } from "../src/tx.js";
import { GuardError, assertChallengeMatches, identityFor, verifyServerConfig } from "../src/guard.js";

const victimEth = "0x1111111111111111111111111111111111111111";
const victimInj = getInjectiveAddress(victimEth);
const victimSub = `${victimEth}000000000000000000000001`;
const attackerEth = "0x2222222222222222222222222222222222222222";
const attackerInj = getInjectiveAddress(attackerEth);
const attackerSub = `${attackerEth}000000000000000000000001`;
const GRANTEE = "inj1hkhdaj2a2clmq5jq6mspsggqs32vynpk228q3r";
const MARKET = `0x${"ab".repeat(32)}`;
const OTHER_MARKET = `0x${"cd".repeat(32)}`;
const PINNED = { testnet: { grantee: GRANTEE, marketId: MARKET }, mainnet: { grantee: GRANTEE, marketId: MARKET } };

function genuine(overrides = {}) {
  return {
    network: "testnet",
    chain_id: "injective-888",
    evm_chain_id: 11155111,
    grantee: GRANTEE,
    market: { ticker: "BTC/USDC PERP", market_id: MARKET, quote_denom: "peggy0xusdc", quote_decimals: 6 },
    subaccount_nonce: 1,
    grant_ttl_seconds: 30 * 86400,
    grants: ALLOWED_GRANTS.map(({ authz_type_url, msg_type_url }) => ({ authz_type_url, msg_type_url, permits: "server text" })),
    ...overrides,
  };
}

function refused(fn, code = "CONFIG_REFUSED") {
  assert.throws(fn, (error) => error instanceof GuardError && error.code === code);
}

test("a genuine config passes and only compiled labels reach the page", () => {
  const checked = verifyServerConfig(genuine(), PINNED);
  assert.equal(checked.grantee, GRANTEE);
  assert.equal(checked.marketId, MARKET);
  assert.equal(checked.chainId, "injective-888");
  assert.equal(checked.evmChainId, 11155111);
  assert.deepEqual(checked.grants.map((g) => g.permits), ALLOWED_GRANTS.map((g) => g.permits));
  assert.ok(checked.grants.every((g) => g.permits !== "server text"));
});

test("an attacker grantee is refused", () => {
  refused(() => verifyServerConfig(genuine({ grantee: attackerInj }), PINNED));
});

test("another market is refused", () => {
  refused(() => verifyServerConfig(genuine({ market: { ticker: "X", market_id: OTHER_MARKET, quote_denom: "d" } }), PINNED));
});

test("an authz type outside the compiled list is refused, even with a friendly label", () => {
  const hostile = [
    { authz_type_url: "/injective.exchange.v2.CreateSpotMarketOrderAuthz", msg_type_url: "/injective.exchange.v2.MsgCreateSpotMarketOrder", permits: "Place market orders" },
  ];
  refused(() => verifyServerConfig(genuine({ grants: hostile }), PINNED));
  const relabelled = [
    { authz_type_url: "/injective.exchange.v2.BatchCreateDerivativeLimitOrdersAuthz", msg_type_url: "/injective.exchange.v2.MsgBatchCreateDerivativeLimitOrders", permits: "Cancel orders" },
  ];
  refused(() => verifyServerConfig(genuine({ grants: relabelled }), PINNED));
  // The limit-order grant is test-only, issued by scripts/testnet_setup.py and never by this page.
  const limitOrders = [
    { authz_type_url: "/injective.exchange.v2.CreateDerivativeLimitOrderAuthz", msg_type_url: "/injective.exchange.v2.MsgCreateDerivativeLimitOrder", permits: "Place limit orders" },
  ];
  refused(() => verifyServerConfig(genuine({ grants: limitOrders }), PINNED));
});

test("a duplicated grant and an empty grant list are refused", () => {
  const one = ALLOWED_GRANTS[0];
  refused(() => verifyServerConfig(genuine({ grants: [one, one] }), PINNED));
  refused(() => verifyServerConfig(genuine({ grants: [] }), PINNED));
});

test("wrong chain identifiers are refused", () => {
  refused(() => verifyServerConfig(genuine({ chain_id: "injective-1" }), PINNED));
  refused(() => verifyServerConfig(genuine({ evm_chain_id: 1 }), PINNED));
  refused(() => verifyServerConfig(genuine({ network: "elsewhere" }), PINNED));
});

test("the grant expiry is capped at 30 days on the client", () => {
  const checked = verifyServerConfig(genuine({ grant_ttl_seconds: 10 * 365 * 86400 }), PINNED);
  assert.equal(checked.ttlSeconds, MAX_GRANT_TTL_S);
  assert.equal(grantExpiryUnix(1_800_000_000_000, 10 * 365 * 86400), 1_800_000_000 + MAX_GRANT_TTL_S);
  refused(() => verifyServerConfig(genuine({ grant_ttl_seconds: 0 }), PINNED));
  refused(() => verifyServerConfig(genuine({ grant_ttl_seconds: "soon" }), PINNED));
});

test("mainnet with nothing compiled in is refused whatever the server says", () => {
  const mainnet = genuine({ network: "mainnet", chain_id: "injective-1", evm_chain_id: 1 });
  refused(() => verifyServerConfig(mainnet, { testnet: {}, mainnet: {} }));
  refused(() => verifyServerConfig(mainnet, { testnet: {}, mainnet: { grantee: GRANTEE } }));
  assert.equal(verifyServerConfig(mainnet, PINNED).network, "mainnet");
});

test("testnet with nothing compiled in runs on what the server says, but only inside the allowlist", () => {
  const checked = verifyServerConfig(genuine(), { testnet: {}, mainnet: {} });
  assert.equal(checked.grantee, GRANTEE);
  refused(() => verifyServerConfig(genuine({ grants: [{ authz_type_url: "/x", msg_type_url: "/y" }] }), { testnet: {}, mainnet: {} }));
});

test("the identity is computed locally", () => {
  assert.deepEqual(identityFor(victimEth), { eth: victimEth, inj: victimInj, sub: victimSub });
});

test("a start response naming another subaccount or address is refused before signing", () => {
  const challenge = (overrides = {}) => ({
    inj_address: victimInj,
    subaccount_id: victimSub,
    message: `halo.test wants you to sign in with your Ethereum account:\n0x1111111111111111111111111111111111111111\n\nStatement.\n\nURI: https://halo.test\nVersion: 1\nChain ID: 11155111\nNonce: abcdef12\nIssued At: 2026-11-09T00:00:00Z\nExpiration Time: 2026-11-09T00:05:00Z\nRequest ID: onboard`,
    ...overrides,
  });
  assertChallengeMatches(challenge(), identityFor(victimEth), "halo.test", "onboard");
  refused(() => assertChallengeMatches(challenge({ subaccount_id: attackerSub }), identityFor(victimEth), "halo.test", "onboard"), "IDENTITY_MISMATCH");
  refused(() => assertChallengeMatches(challenge({ inj_address: attackerInj }), identityFor(victimEth), "halo.test", "onboard"), "IDENTITY_MISMATCH");
  refused(() => assertChallengeMatches(challenge(), identityFor(victimEth), "evil.example", "onboard"), "CHALLENGE_REFUSED");
  refused(() => assertChallengeMatches(challenge(), identityFor(attackerEth), "halo.test", "onboard"), "IDENTITY_MISMATCH");
  refused(() => assertChallengeMatches(challenge(), identityFor(victimEth), "halo.test", "revoke"), "CHALLENGE_REFUSED");
});

test("buildGrantMsgs and buildRevokeMsgs refuse anything outside the compiled list", () => {
  const hostile = [{ authz_type_url: "/injective.exchange.v2.CreateSpotMarketOrderAuthz", msg_type_url: "/injective.exchange.v2.MsgCreateSpotMarketOrder" }];
  assert.throws(() => buildGrantMsgs({ granter: victimInj, grantee: GRANTEE, subaccountId: victimSub, marketId: MARKET, grants: hostile, expiresUnix: 1 }), /Refused authorization/);
  assert.throws(() => buildRevokeMsgs({ granter: victimInj, grantee: GRANTEE, grants: hostile }), /Refused authorization/);
  assert.equal(buildGrantMsgs({ granter: victimInj, grantee: GRANTEE, subaccountId: victimSub, marketId: MARKET, grants: ALLOWED_GRANTS, expiresUnix: 1 }).length, 2);
});
