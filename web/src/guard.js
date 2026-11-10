// Checks the page runs on everything the server sends before anything reaches the wallet, so a
// tampered server response cannot change what the user signs. Pure: also imported by the Node tests.
// The expected values per network are compiled in at build time (web/build.mjs, HALO_EXPECTED_*).
import { getInjectiveAddress } from "@injectivelabs/sdk-ts";
import { ALLOWED_GRANTS, MAX_GRANT_TTL_S } from "./tx.js";

export class GuardError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

const COMPILED = typeof __HALO_EXPECTED__ === "undefined" ? { testnet: {}, mainnet: {} } : __HALO_EXPECTED__;
const SUBACCOUNT_NONCE = 1;
const CHAIN_IDS = {
  testnet: { chainId: "injective-888", evmChainId: 11155111 },
  mainnet: { chainId: "injective-1", evmChainId: 1 },
};
const MARKET_ID_RE = /^0x[0-9a-f]{64}$/;

function refuse() {
  throw new GuardError("CONFIG_REFUSED");
}

/** Checks /onboard/config against the compiled expectations. Returns only what may be signed. */
export function verifyServerConfig(config, expected = COMPILED) {
  const chain = CHAIN_IDS[config.network];
  if (!chain || config.chain_id !== chain.chainId || Number(config.evm_chain_id) !== chain.evmChainId) refuse();
  const want = (expected && expected[config.network]) || {};
  // A mainnet page must have its grantee and market fixed at build time: no server response decides them.
  if (config.network === "mainnet" && (!want.grantee || !want.marketId)) refuse();
  if (want.grantee && config.grantee !== want.grantee) refuse();
  if (want.marketId && config.market.market_id !== want.marketId) refuse();
  if (!config.grantee || !MARKET_ID_RE.test(config.market.market_id)) refuse();
  if (config.subaccount_nonce !== SUBACCOUNT_NONCE) refuse();
  if (!Array.isArray(config.grants) || config.grants.length === 0) refuse();
  const grants = config.grants.map((grant) => {
    const allowed = ALLOWED_GRANTS.find(
      (entry) => entry.authz_type_url === grant.authz_type_url && entry.msg_type_url === grant.msg_type_url,
    );
    if (!allowed) refuse();
    return allowed;
  });
  if (new Set(grants.map((grant) => grant.authz_type_url)).size !== grants.length) refuse();
  if (!Number.isInteger(config.grant_ttl_seconds) || config.grant_ttl_seconds <= 0) refuse();
  return {
    network: config.network,
    chainId: chain.chainId,
    evmChainId: chain.evmChainId,
    grantee: config.grantee,
    marketId: config.market.market_id,
    grants,
    ttlSeconds: Math.min(config.grant_ttl_seconds, MAX_GRANT_TTL_S),
  };
}

/** The Injective address and trading subaccount of a wallet, computed here and never taken from the server. */
export function identityFor(eth) {
  const lower = String(eth).toLowerCase();
  return { eth: lower, inj: getInjectiveAddress(lower), sub: `${lower}${SUBACCOUNT_NONCE.toString(16).padStart(24, "0")}` };
}

/** Refuses a sign-in challenge that does not match this wallet, this site and this action. */
export function assertChallengeMatches(info, identity, host, action) {
  if (info.inj_address !== identity.inj || info.subaccount_id !== identity.sub) throw new GuardError("IDENTITY_MISMATCH");
  const lines = String(info.message).split("\n");
  if (lines[0] !== `${host} wants you to sign in with your Ethereum account:`) throw new GuardError("CHALLENGE_REFUSED");
  if ((lines[1] || "").toLowerCase() !== identity.eth) throw new GuardError("CHALLENGE_REFUSED");
  if (!lines.includes(`Request ID: ${action}`)) throw new GuardError("CHALLENGE_REFUSED");
}
