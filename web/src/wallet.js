// Everything that talks to window.ethereum.

export class WalletError extends Error {
  constructor(code, message) {
    super(message || code);
    this.code = code;
  }
}

export function getProvider() {
  const provider = window.ethereum;
  if (!provider) throw new WalletError("NO_WALLET");
  return provider;
}

export function shortAddress(address) {
  return address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "";
}

export async function connectWallet() {
  const provider = getProvider();
  let accounts;
  try {
    accounts = await provider.request({ method: "eth_requestAccounts" });
  } catch (error) {
    throw toWalletError(error, "CONNECT_REJECTED");
  }
  if (!accounts || accounts.length === 0) throw new WalletError("NO_ACCOUNT");
  return accounts[0];
}

function toWalletError(error, rejectedCode) {
  if (error && error.code === 4001) return new WalletError(rejectedCode);
  return new WalletError("WALLET_ERROR", error && error.message ? error.message : undefined);
}

export async function ensureEvmChain(evmChainId) {
  const provider = getProvider();
  const target = `0x${evmChainId.toString(16)}`;
  const current = await provider.request({ method: "eth_chainId" });
  if (current && parseInt(current, 16) === evmChainId) return;
  try {
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: target }] });
  } catch (error) {
    throw toWalletError(error, "SWITCH_REJECTED");
  }
}

export async function personalSign(address, message) {
  const provider = getProvider();
  const hex = `0x${Array.from(new TextEncoder().encode(message), (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
  try {
    return await provider.request({ method: "personal_sign", params: [hex, address] });
  } catch (error) {
    throw toWalletError(error, "SIGN_REJECTED");
  }
}

export async function signTypedData(address, typedData) {
  const provider = getProvider();
  try {
    return await provider.request({
      method: "eth_signTypedData_v4",
      params: [address, JSON.stringify(typedData)],
    });
  } catch (error) {
    throw toWalletError(error, "SIGN_REJECTED");
  }
}
