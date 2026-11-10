// Read-only dry run: asks an Injective TESTNET node to simulate the exact grant, deposit and revoke
// transactions the page builds. Nothing is broadcast and no key signs for real. Simulation skips
// signature checks, so this proves message decoding, validation and handler acceptance only.
import { Wallet } from "ethers";
import { ChainRestAuthApi, BaseAccount, TxClient, getEthereumAddress } from "@injectivelabs/sdk-ts";
import {
  GAS_DEPOSIT, GAS_GRANT, GAS_REVOKE, assembleTx, buildDepositMsg, buildGrantMsgs, buildRevokeMsgs,
  buildTypedData, feeFor, grantExpiryUnix,
} from "../src/tx.js";

const REST = process.env.HALO_SIMULATE_LCD || "https://testnet.sentry.lcd.injective.network";
const CHAIN_ID = "injective-888";
if (process.env.HALO_SIMULATE_NETWORK && process.env.HALO_SIMULATE_NETWORK !== "testnet") {
  throw new Error("This script is testnet only.");
}
const EVM_CHAIN_ID = 11155111;
const GRANTEE = process.env.HALO_GRANTEE || "inj1hkhdaj2a2clmq5jq6mspsggqs32vynpk228q3r";
const MARKET = process.env.HALO_MARKET_ID || "0x4ca0f92fc28be0c9761326016b5a1a2177dd6375558365116b5bdda9abc229ce";
const USDC = process.env.HALO_USDC_DENOM || "inj"; // Deposit denom is only used for the shape check.
const GRANTS = [
  { authz_type_url: "/injective.exchange.v2.CreateDerivativeMarketOrderAuthz", msg_type_url: "/injective.exchange.v2.MsgCreateDerivativeMarketOrder" },
  { authz_type_url: "/injective.exchange.v2.CancelDerivativeOrderAuthz", msg_type_url: "/injective.exchange.v2.MsgCancelDerivativeOrder" },
];

async function retryFetch(url, options) {
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await fetch(url, options);
    } catch (error) {
      if (attempt >= 4) throw error;
      await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
    }
  }
}

async function pickAccount() {
  const fixed = process.env.HALO_SIMULATE_ADDRESS;
  if (fixed) return new ChainRestAuthApi(REST).fetchAccount(fixed).then((a) => BaseAccount.fromRestApi(a));
  // Any funded account works: simulation needs a real account number and sequence, not its key.
  const response = await retryFetch(`${REST}/cosmos/auth/v1beta1/accounts?pagination.limit=200&pagination.reverse=true`);
  const body = await response.json();
  for (const item of body.accounts) {
    const base = item.base_account;
    if (!base || !base.pub_key || !base.address.startsWith("inj1")) continue;
    const balance = await retryFetch(`${REST}/cosmos/bank/v1beta1/balances/${base.address}/by_denom?denom=inj`).then((r) => r.json());
    if (BigInt(balance.balance?.amount || "0") >= 10n ** 15n) return BaseAccount.fromRestApi({ account: item });
  }
  throw new Error("no funded account with a public key in the first page");
}

async function main() {
  const account = await pickAccount();
  const inj = account.address || account.bech32Address;
  const eth = getEthereumAddress(inj);
  const sub = `${eth.toLowerCase()}000000000000000000000001`;
  const height = await (await retryFetch(`${REST}/cosmos/base/tendermint/v1beta1/blocks/latest`)).json();
  const timeoutHeight = BigInt(height.block.header.height) + 600n;
  const params = { chainId: CHAIN_ID, evmChainId: EVM_CHAIN_ID, accountNumber: account.accountNumber, sequence: account.sequence, timeoutHeight };

  const cases = {
    grant: { msgs: buildGrantMsgs({ granter: inj, grantee: GRANTEE, subaccountId: sub, marketId: MARKET, grants: GRANTS, expiresUnix: grantExpiryUnix(Date.now(), 86400) }), gas: GAS_GRANT },
    deposit: { msgs: buildDepositMsg({ injAddress: inj, subaccountId: sub, denom: USDC, amount: "1" }), gas: GAS_DEPOSIT },
    revoke: { msgs: buildRevokeMsgs({ granter: inj, grantee: GRANTEE, grants: GRANTS }), gas: GAS_REVOKE },
  };

  console.log(`address ${inj} account ${account.accountNumber} sequence ${account.sequence} pubkey ${account.pubKey.type}`);
  for (const [name, { msgs, gas }] of Object.entries(cases)) {
    const fee = feeFor(gas, "160000000");
    const typedData = buildTypedData({ msgs, ...params, fee });
    const throwaway = Wallet.createRandom();
    const digestSig = await throwaway.signMessage(typedData.message.msgs);
    const txRaw = await assembleTx({ msgs, ...params, fee, typedData, signature: digestSig, accountPubKeyBase64: account.pubKey.key });
    if (process.env.HALO_SIMULATE_DUMP) {
      console.log(`DUMP ${name} ${TxClient.encode(txRaw)}`);
      continue;
    }
    const response = await retryFetch(`${REST}/cosmos/tx/v1beta1/simulate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tx_bytes: TxClient.encode(txRaw) }),
    });
    const text = await response.text();
    if (response.ok) {
      const gasUsed = JSON.parse(text).gas_info.gas_used;
      console.log(`${name}: ACCEPTED gas_used=${gasUsed}`);
    } else {
      console.log(`${name}: REJECTED status=${response.status} ${text.slice(0, 700)}`);
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
