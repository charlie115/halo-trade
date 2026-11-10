// Signs the exact EIP-712 payloads the page sends to MetaMask with a throwaway key, and checks that
// they recover and match what the chain and docs/GRANTS.md require. No network access.
import assert from "node:assert/strict";
import { test } from "node:test";
import { Wallet, TypedDataEncoder, concat, hexlify, keccak256, recoverAddress, verifyMessage } from "ethers";
import { SignTypedDataVersion, TypedDataUtils, signTypedData } from "@metamask/eth-sig-util";
import * as CosmosAuthzTx from "@injectivelabs/core-proto-ts-v2/generated/cosmos/authz/v1beta1/tx_pb.js";
import * as CosmosTx from "@injectivelabs/core-proto-ts-v2/generated/cosmos/tx/v1beta1/tx_pb.js";
import * as ExchangeAuthz from "@injectivelabs/core-proto-ts-v2/generated/injective/exchange/v2/authz_pb.js";
import * as ExchangeTx from "@injectivelabs/core-proto-ts-v2/generated/injective/exchange/v2/tx_pb.js";
import * as InjTxExt from "@injectivelabs/core-proto-ts-v2/generated/injective/types/v1beta1/tx_ext_pb.js";
import { TxClient, getInjectiveAddress, hexToBuff } from "@injectivelabs/sdk-ts";
import {
  GAS_DEPOSIT,
  GAS_GRANT,
  GAS_REVOKE,
  GAS_WITHDRAW,
  ALLOWED_GRANTS,
  assembleTx,
  buildDepositMsg,
  buildGrantMsgs,
  buildRevokeMsgs,
  buildTypedData,
  buildWithdrawMsg,
  feeFor,
  fromBaseUnits,
  grantExpiryUnix,
  toBaseUnits,
} from "../src/tx.js";

// Throwaway test key from a fresh mnemonic; never funded, never used on any network.
const wallet = Wallet.createRandom();
const ethAddress = wallet.address.toLowerCase();
const injAddress = getInjectiveAddress(ethAddress);
const subaccountId = `${ethAddress}000000000000000000000001`.replace(/^0x/, "0x");
const grantee = "inj1hkhdaj2a2clmq5jq6mspsggqs32vynpk228q3r";
const marketId = "0x4ca0f92fc28be0c9761326016b5a1a2177dd6375558365116b5bdda9abc229ce";
const CHAIN_ID = "injective-888";
const EVM_CHAIN_ID = 11155111;

// The list `chain/grants.py` and docs/GRANTS.md require, verbatim.
const GRANTS = [
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

const EXPIRES = 1796774400; // 2026-12-09T00:00:00Z

function txParams(fee) {
  return {
    chainId: CHAIN_ID,
    evmChainId: EVM_CHAIN_ID,
    accountNumber: 1234,
    sequence: 7,
    timeoutHeight: 147700000n,
    fee,
  };
}

/** EIP-712 digest, computed independently with ethers' struct hashing (no salt restrictions). */
function ethersDigest(typedData) {
  const { EIP712Domain, ...types } = typedData.types;
  const domainSeparator = TypedDataEncoder.hashStruct("EIP712Domain", { EIP712Domain }, typedData.domain);
  const message = TypedDataEncoder.hashStruct(typedData.primaryType, types, typedData.message);
  return keccak256(concat(["0x1901", domainSeparator, message]));
}

function signAndRecover(typedData) {
  const digest = ethersDigest(typedData);
  const signature = wallet.signingKey.sign(digest).serialized;
  assert.equal(recoverAddress(digest, signature).toLowerCase(), ethAddress, "ethers signature recovers the signer");

  // MetaMask's own signing library must compute the same digest and the same signature.
  const data = JSON.parse(JSON.stringify(typedData));
  const metamaskDigest = hexlify(TypedDataUtils.eip712Hash(data, SignTypedDataVersion.V4));
  assert.equal(metamaskDigest, digest, "MetaMask computes the same digest as ethers");
  const metamaskSignature = signTypedData({
    privateKey: Buffer.from(wallet.privateKey.slice(2), "hex"),
    data,
    version: SignTypedDataVersion.V4,
  });
  assert.equal(metamaskSignature, signature, "MetaMask signature is byte-identical");
  return signature;
}

function checkDomainAndTypes(typedData) {
  assert.deepEqual(typedData.domain, {
    name: "Injective Web3",
    version: "1.0.0",
    chainId: "0xaa36a7",
    verifyingContract: "0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC",
    salt: "0",
  });
  assert.equal(typedData.primaryType, "Tx");
  assert.deepEqual(typedData.types.Tx, [
    { name: "context", type: "string" },
    { name: "msgs", type: "string" },
  ]);
  assert.deepEqual(typedData.types.EIP712Domain, [
    { name: "name", type: "string" },
    { name: "version", type: "string" },
    { name: "chainId", type: "uint256" },
    { name: "verifyingContract", type: "address" },
    { name: "salt", type: "string" },
  ]);
}

test("deposit: typed data has the exact chain shape and the signature recovers", async () => {
  const msg = buildDepositMsg({ injAddress, subaccountId, denom: "peggy0xUSDC", amount: "50500000" });
  const fee = feeFor(GAS_DEPOSIT, "160000000.000000000000000000");
  const typedData = buildTypedData({ msgs: msg, ...txParams(fee) });
  checkDomainAndTypes(typedData);

  const context = JSON.parse(typedData.message.context);
  assert.deepEqual(context, {
    account_number: 1234,
    chain_id: CHAIN_ID,
    fee: { amount: [{ denom: "inj", amount: String(BigInt(GAS_DEPOSIT) * 160000000n) }], gas: Number(GAS_DEPOSIT) },
    memo: "",
    sequence: 7,
    timeout_height: 147700000,
  });

  assert.equal(
    typedData.message.msgs,
    JSON.stringify([
      {
        "@type": "/injective.exchange.v2.MsgDeposit",
        sender: injAddress,
        subaccount_id: subaccountId,
        amount: { denom: "peggy0xUSDC", amount: "50500000" },
      },
    ]),
  );

  const signature = signAndRecover(typedData);
  const txRaw = await assembleTx({ msgs: msg, ...txParams(fee), typedData, signature, accountPubKeyBase64: null });
  const body = CosmosTx.TxBody.fromBinary(txRaw.bodyBytes);
  assert.equal(body.messages.length, 1);
  assert.equal(body.messages[0].typeUrl, "/injective.exchange.v2.MsgDeposit");
  const deposit = ExchangeTx.MsgDeposit.fromBinary(body.messages[0].value);
  assert.equal(deposit.sender, injAddress);
  assert.equal(deposit.subaccountId, subaccountId);
  assert.deepEqual(deposit.amount, { denom: "peggy0xUSDC", amount: "50500000" });
  assert.equal(txRaw.signatures[0].length, 65);
});

test("withdraw: typed data has the exact chain shape, the signature recovers, and it pays the signer", async () => {
  const msg = buildWithdrawMsg({ injAddress, subaccountId, denom: "peggy0xUSDC", amount: "25500000" });
  const fee = feeFor(GAS_WITHDRAW, "160000000.000000000000000000");
  const typedData = buildTypedData({ msgs: msg, ...txParams(fee) });
  checkDomainAndTypes(typedData);

  const context = JSON.parse(typedData.message.context);
  assert.equal(context.chain_id, CHAIN_ID);
  assert.equal(context.account_number, 1234);
  assert.deepEqual(context.fee, { amount: [{ denom: "inj", amount: String(BigInt(GAS_WITHDRAW) * 160000000n) }], gas: Number(GAS_WITHDRAW) });
  const signedMsgs = JSON.parse(typedData.message.msgs);
  assert.equal(signedMsgs.length, 1);
  assert.equal(signedMsgs[0]["@type"], "/injective.exchange.v2.MsgWithdraw");
  // The chain pays the sender, and there is no other address in what is signed.
  assert.deepEqual(Object.keys(signedMsgs[0]).sort(), ["@type", "amount", "sender", "subaccount_id"]);
  assert.equal(signedMsgs[0].sender, injAddress);
  assert.equal(signedMsgs[0].subaccount_id, subaccountId);
  assert.deepEqual(signedMsgs[0].amount, { denom: "peggy0xUSDC", amount: "25500000" });

  const signature = signAndRecover(typedData);
  const txRaw = await assembleTx({ msgs: msg, ...txParams(fee), typedData, signature, accountPubKeyBase64: null });
  const body = CosmosTx.TxBody.fromBinary(txRaw.bodyBytes);
  assert.equal(body.messages.length, 1);
  assert.equal(body.messages[0].typeUrl, "/injective.exchange.v2.MsgWithdraw");
  const withdraw = ExchangeTx.MsgWithdraw.fromBinary(body.messages[0].value);
  assert.equal(withdraw.sender, injAddress);
  assert.equal(withdraw.subaccountId, subaccountId);
  assert.deepEqual(withdraw.amount, { denom: "peggy0xUSDC", amount: "25500000" });
  assert.equal(body.extensionOptions[0].typeUrl, "/injective.types.v1beta1.ExtensionOptionsWeb3Tx");
  assert.equal(txRaw.signatures[0].length, 65);
});

test("withdraw refuses a subaccount that is not the signer's", () => {
  const other = Wallet.createRandom().address.toLowerCase();
  assert.throws(
    () => buildWithdrawMsg({ injAddress, subaccountId: `${other}000000000000000000000001`, denom: "peggy0xUSDC", amount: "1" }),
    /another address/,
  );
});

test("the server's grant list has nothing that can withdraw, and cannot be made to", () => {
  const text = JSON.stringify(ALLOWED_GRANTS).toLowerCase();
  assert.ok(!text.includes("withdraw") && !text.includes("msgsend") && !text.includes("transfer"));
  assert.throws(() => buildGrantMsgs({
    granter: injAddress, grantee, subaccountId, marketId, expiresUnix: EXPIRES,
    grants: [{ authz_type_url: "/cosmos.authz.v1beta1.GenericAuthorization", msg_type_url: "/injective.exchange.v2.MsgWithdraw" }],
  }), /Refused authorization/);
});

test("grant: two v2 typed authorizations limited to one subaccount and one market", async () => {
  const msgs = buildGrantMsgs({ granter: injAddress, grantee, subaccountId, marketId, grants: GRANTS, expiresUnix: EXPIRES });
  assert.equal(msgs.length, 2);

  const fee = feeFor(GAS_GRANT, "160000000");
  const typedData = buildTypedData({ msgs, ...txParams(fee) });
  checkDomainAndTypes(typedData);

  const expected = GRANTS.map((grant) => ({
    "@type": "/cosmos.authz.v1beta1.MsgGrant",
    granter: injAddress,
    grantee,
    grant: {
      authorization: { "@type": grant.authz_type_url, subaccount_id: subaccountId, market_ids: [marketId] },
      expiration: "2026-12-09T00:00:00Z",
    },
  }));
  // Compared as a string: key order is part of what the signature covers.
  assert.equal(typedData.message.msgs, JSON.stringify(expected));

  const parsed = JSON.parse(typedData.message.msgs);
  for (const message of parsed) {
    const url = message.grant.authorization["@type"];
    assert.ok(url.startsWith("/injective.exchange.v2.") && url.endsWith("Authz"), url);
    assert.ok(!/withdraw|transfer|send|batch|generic/i.test(url), `no transfer or generic permission: ${url}`);
  }
  assert.ok(!/GenericAuthorization|MsgSend|MsgWithdraw|MsgExternalTransfer|MsgSubaccountTransfer/.test(typedData.message.msgs));

  const signature = signAndRecover(typedData);
  const txRaw = await assembleTx({ msgs, ...txParams(fee), typedData, signature, accountPubKeyBase64: null });

  const body = CosmosTx.TxBody.fromBinary(txRaw.bodyBytes);
  assert.equal(body.messages.length, 2);
  assert.equal(body.timeoutHeight, 147700000n);
  body.messages.forEach((any, index) => {
    assert.equal(any.typeUrl, "/cosmos.authz.v1beta1.MsgGrant");
    const grant = CosmosAuthzTx.MsgGrant.fromBinary(any.value);
    assert.equal(grant.granter, injAddress);
    assert.equal(grant.grantee, grantee);
    assert.equal(grant.grant.authorization.typeUrl, GRANTS[index].authz_type_url);
    const name = GRANTS[index].authz_type_url.split(".").pop();
    const authz = ExchangeAuthz[name].fromBinary(grant.grant.authorization.value);
    assert.equal(authz.subaccountId, subaccountId);
    assert.deepEqual(authz.marketIds, [marketId]);
    assert.equal(grant.grant.expiration.seconds, BigInt(EXPIRES));
  });

  assert.equal(body.extensionOptions.length, 1);
  assert.equal(body.extensionOptions[0].typeUrl, "/injective.types.v1beta1.ExtensionOptionsWeb3Tx");
  const extension = InjTxExt.ExtensionOptionsWeb3Tx.fromBinary(body.extensionOptions[0].value);
  assert.equal(extension.typedDataChainID, BigInt(EVM_CHAIN_ID));

  const authInfo = CosmosTx.AuthInfo.fromBinary(txRaw.authInfoBytes);
  assert.equal(authInfo.signerInfos.length, 1);
  assert.equal(authInfo.signerInfos[0].sequence, 7n);
  assert.equal(authInfo.signerInfos[0].publicKey.typeUrl, "/injective.crypto.v1beta1.ethsecp256k1.PubKey");
  assert.equal(txRaw.signatures[0].length, 65);
  assert.ok(/^[0-9A-F]{64}$/.test(TxClient.hash(txRaw)));
});

test("revoke: one MsgRevoke per granted message type", async () => {
  const msgs = buildRevokeMsgs({ granter: injAddress, grantee, grants: GRANTS });
  assert.equal(msgs.length, 2);
  const fee = feeFor(GAS_REVOKE, null);
  const typedData = buildTypedData({ msgs, ...txParams(fee) });
  checkDomainAndTypes(typedData);
  assert.equal(
    typedData.message.msgs,
    JSON.stringify(
      GRANTS.map((grant) => ({
        "@type": "/cosmos.authz.v1beta1.MsgRevoke",
        granter: injAddress,
        grantee,
        msg_type_url: grant.msg_type_url,
      })),
    ),
  );
  signAndRecover(typedData);
});

test("an unknown authorization type refuses to build", () => {
  assert.throws(() =>
    buildGrantMsgs({
      granter: injAddress,
      grantee,
      subaccountId,
      marketId,
      grants: [{ authz_type_url: "/injective.exchange.v2.DoesNotExist", msg_type_url: "x" }],
      expiresUnix: EXPIRES,
    }).map((m) => m.toEip712V2()),
  );
});

test("the personal_sign proof text recovers through ethers", async () => {
  const signature = await wallet.signMessage("halo-trade wallet proof");
  assert.equal(verifyMessage("halo-trade wallet proof", signature).toLowerCase(), ethAddress);
});

test("amount parsing is exact and refuses more precision than the token has", () => {
  assert.equal(toBaseUnits("50.5", 6), 50_500_000n);
  assert.equal(toBaseUnits("0.000001", 6), 1n);
  assert.equal(toBaseUnits("1.0000001", 6), null);
  assert.equal(toBaseUnits("-1", 6), null);
  assert.equal(toBaseUnits("abc", 6), null);
  assert.equal(fromBaseUnits(50_500_000n, 6), "50.5");
  assert.equal(fromBaseUnits(1n, 6), "0.000001");
});

test("gas price never falls below the chain minimum", () => {
  const low = feeFor(GAS_GRANT, "100000000");
  assert.equal(low.amount[0].amount, String(BigInt(GAS_GRANT) * 160000000n));
  const high = feeFor(GAS_GRANT, "200000000.000000000000000000");
  assert.equal(high.amount[0].amount, String(BigInt(GAS_GRANT) * 200000000n));
});

test("grant expiry is ttl seconds ahead of now", () => {
  assert.equal(grantExpiryUnix(1_700_000_000_123, 2_592_000), 1_702_592_000);
  void hexToBuff;
});
