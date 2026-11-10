// Reads and broadcasts through the public Injective LCD. Reads are plain fetch; the broadcast
// uses the SDK client so its inclusion handling matches other Injective apps.
import { TxRestApi, BaseAccount, ChainRestAuthApi } from "@injectivelabs/sdk-ts";

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const error = new Error(`LCD ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export async function fetchAccount(rest, injAddress) {
  try {
    const api = new ChainRestAuthApi(rest);
    return BaseAccount.fromRestApi(await api.fetchAccount(injAddress));
  } catch (error) {
    if (error && (error.status === 404 || /not found/i.test(String(error.message)))) return null;
    throw error;
  }
}

export async function fetchLatestHeight(rest) {
  const body = await getJson(`${rest}/cosmos/base/tendermint/v1beta1/blocks/latest`);
  return BigInt(body.block.header.height);
}

export async function fetchBaseFee(rest) {
  try {
    const body = await getJson(`${rest}/injective/txfees/v1beta1/cur_eip_base_fee`);
    return body.base_fee && body.base_fee.base_fee ? body.base_fee.base_fee : null;
  } catch {
    return null; // The minimum gas price still applies.
  }
}

export async function fetchBankBalance(rest, injAddress, denom) {
  const url = `${rest}/cosmos/bank/v1beta1/balances/${injAddress}/by_denom?denom=${encodeURIComponent(denom)}`;
  const body = await getJson(url);
  return BigInt(body.balance && body.balance.amount ? body.balance.amount : "0");
}

export async function broadcast(rest, txRaw) {
  const api = new TxRestApi(rest, { timeout: 60000 });
  return api.broadcast(txRaw);
}
