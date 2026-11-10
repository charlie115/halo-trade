// Pure transaction construction. Imported by the page and by the Node signing test,
// so that the payload a test signs is exactly the payload a wallet signs.
// Deep imports: the package index references generated files it does not ship.
import * as InjectiveExchangeV2AuthzPb from "@injectivelabs/core-proto-ts-v2/generated/injective/exchange/v2/authz_pb.js";
import { Any } from "@injectivelabs/core-proto-ts-v2/generated/google/protobuf/any_pb.js";
import {
  MsgDepositV2,
  MsgGrantWithAuthorization,
  MsgRevoke,
  MsgWithdrawV2,
  SIGN_EIP712_V2,
  createTransaction,
  createTxRawEIP712,
  createWeb3Extension,
  getEip712TypedDataV2,
  getEthereumAddress,
  hexToBase64,
  hexToBuff,
  recoverTypedSignaturePubKey,
} from "@injectivelabs/sdk-ts";
import { getStdFee } from "@injectivelabs/utils";

// Fixed limits about 1.7x above testnet simulation (grant with three messages 179k, deposit 298k gas used on 2026-11-08).
// The fee at these limits is well under 0.001 INJ.
export const GAS_DEPOSIT = "500000";
export const GAS_GRANT = "600000";
export const GAS_REVOKE = "450000";
export const GAS_WITHDRAW = "500000"; // the deposit's limit: not yet simulated on testnet
export const MIN_GAS_PRICE = 160000000n;
export const MAX_GRANT_TTL_S = 30 * 24 * 3600;

// Compiled into the bundle: no server response can add a permission to what the wallet signs. The
// labels are what the page shows. Keep in step with docs/GRANTS.md and chain/grants.py.
export const ALLOWED_GRANTS = [
  {
    authz_type_url: "/injective.exchange.v2.CreateDerivativeMarketOrderAuthz",
    msg_type_url: "/injective.exchange.v2.MsgCreateDerivativeMarketOrder",
    permits: "Place market, stop and take-profit orders",
  },
  {
    authz_type_url: "/injective.exchange.v2.CancelDerivativeOrderAuthz",
    msg_type_url: "/injective.exchange.v2.MsgCancelDerivativeOrder",
    permits: "Cancel orders",
  },
];

function assertAllowedGrants(grants) {
  for (const grant of grants) {
    if (!ALLOWED_GRANTS.some((entry) => entry.authz_type_url === grant.authz_type_url && entry.msg_type_url === grant.msg_type_url)) {
      throw new Error(`Refused authorization: ${grant.authz_type_url}`);
    }
  }
}

/** Typed exchange authorization, one per order action, bound to one subaccount and one market. */
export class ExchangeOrderAuthz {
  constructor({ authzTypeUrl, subaccountId, marketId }) {
    this.authzTypeUrl = authzTypeUrl;
    this.subaccountId = subaccountId;
    this.marketId = marketId;
    const name = authzTypeUrl.split(".").pop();
    const proto = InjectiveExchangeV2AuthzPb[name];
    if (!proto) throw new Error(`Unknown authorization type: ${authzTypeUrl}`);
    this.proto = proto;
    this.name = name;
  }

  toAny() {
    const value = this.proto.toBinary(
      this.proto.create({ subaccountId: this.subaccountId, marketIds: [this.marketId] }),
    );
    return Any.create({ typeUrl: this.authzTypeUrl, value });
  }

  toAmino() {
    return {
      type: `exchange/${this.name}`,
      value: { subaccount_id: this.subaccountId, market_ids: [this.marketId] },
    };
  }

  toWeb3() {
    return {
      "@type": this.authzTypeUrl,
      subaccount_id: this.subaccountId,
      market_ids: [this.marketId],
    };
  }
}

export function grantExpiryUnix(nowMs, ttlSeconds) {
  return Math.floor(nowMs / 1000) + Math.min(ttlSeconds, MAX_GRANT_TTL_S);
}

export function buildDepositMsg({ injAddress, subaccountId, denom, amount }) {
  return MsgDepositV2.fromJSON({
    injectiveAddress: injAddress,
    subaccountId,
    amount: { denom, amount },
  });
}

/** The chain pays a withdrawal to its sender. There is no destination field for anyone to change. */
export function buildWithdrawMsg({ injAddress, subaccountId, denom, amount }) {
  if (!String(subaccountId).toLowerCase().startsWith(getEthereumAddress(injAddress).toLowerCase())) {
    throw new Error("Refused: that subaccount belongs to another address");
  }
  return MsgWithdrawV2.fromJSON({
    injectiveAddress: injAddress,
    subaccountId,
    amount: { denom, amount },
  });
}

export function buildGrantMsgs({ granter, grantee, subaccountId, marketId, grants, expiresUnix }) {
  assertAllowedGrants(grants);
  return grants.map((grant) =>
    MsgGrantWithAuthorization.fromJSON({
      granter,
      grantee,
      expiration: expiresUnix,
      authorization: new ExchangeOrderAuthz({
        authzTypeUrl: grant.authz_type_url,
        subaccountId,
        marketId,
      }),
    }),
  );
}

export function buildRevokeMsgs({ granter, grantee, grants }) {
  assertAllowedGrants(grants);
  return grants.map((grant) =>
    MsgRevoke.fromJSON({ granter, grantee, messageType: grant.msg_type_url }),
  );
}

export function feeFor(gas, baseFee) {
  let price = MIN_GAS_PRICE;
  if (baseFee) {
    const parsed = BigInt(String(baseFee).split(".")[0]);
    if (parsed > price) price = parsed;
  }
  return getStdFee({ gas, gasPrice: price.toString() });
}

export function buildTypedData({ msgs, chainId, evmChainId, accountNumber, sequence, timeoutHeight, fee, memo = "" }) {
  return getEip712TypedDataV2({
    msgs,
    fee,
    tx: {
      memo,
      accountNumber: String(accountNumber),
      sequence: String(sequence),
      timeoutHeight: String(timeoutHeight),
      chainId,
    },
    evmChainId,
  });
}

/** Attach the wallet signature and return the raw Injective transaction. */
export async function assembleTx({
  msgs,
  chainId,
  evmChainId,
  accountNumber,
  sequence,
  timeoutHeight,
  fee,
  memo = "",
  typedData,
  signature,
  accountPubKeyBase64,
}) {
  const pubKey = accountPubKeyBase64 || hexToBase64(await recoverTypedSignaturePubKey(typedData, signature));
  const { txRaw } = createTransaction({
    message: msgs,
    memo,
    signMode: SIGN_EIP712_V2,
    fee,
    pubKey,
    sequence,
    timeoutHeight: Number(timeoutHeight),
    accountNumber,
    chainId,
  });
  const signed = createTxRawEIP712(txRaw, createWeb3Extension({ evmChainId }));
  signed.signatures = [hexToBuff(signature)];
  return signed;
}

/** "123.45" with `decimals` fractional digits to the integer base amount, or null if invalid. */
export function toBaseUnits(input, decimals) {
  const text = String(input).trim();
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  const [whole, frac = ""] = text.split(".");
  if (frac.length > decimals) return null;
  return BigInt(whole + frac.padEnd(decimals, "0"));
}

export function fromBaseUnits(amount, decimals) {
  const value = amount.toString().padStart(decimals + 1, "0");
  const whole = value.slice(0, value.length - decimals);
  const frac = value.slice(value.length - decimals).replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : whole;
}
