import { NearConnector } from "@hot-labs/near-connect"
import {
  createNearWalletChallenge,
  verifyNearWalletSignature,
} from "./service"

export type NearNetwork = "mainnet" | "testnet"

export const NEAR_NETWORK: NearNetwork =
  import.meta.env?.VITE_NEAR_NETWORK === "mainnet" ||
  import.meta.env?.VITE_NEAR_NETWORK === "testnet"
    ? import.meta.env.VITE_NEAR_NETWORK
    : import.meta.env?.DEV
      ? "testnet"
      : "mainnet"

export const NEAR_CHAIN_IDS: Record<NearNetwork, number> = {
  mainnet: 1,
  testnet: 2,
}

const METEOR_MANIFEST = {
  id: "meteor-wallet",
  name: "Meteor Wallet",
  icon: "https://raw.githubusercontent.com/Meteor-Wallet/meteor_wallet_sdk/main/assets/meteor-logo-svg.svg",
  description: "Meteor Wallet",
  website: "https://meteorwallet.app/",
  version: "1.1.1",
  executor:
    "https://raw.githubusercontent.com/Meteor-Wallet/meteor_wallet_sdk/data-storage/storage/meteor-near-connect-latest.js",
  type: "sandbox",
  features: {
    signMessage: true,
    signInWithoutAddKey: true,
    signInAndSignMessage: true,
    signAndSendTransaction: true,
    signAndSendTransactions: true,
    signInWithFunctionCallKey: true,
    mainnet: true,
    testnet: true,
  },
  platform: { web: "https://wallet.meteorwallet.app" },
  permissions: {
    storage: true,
    allowsOpen: [
      "https://chromewebstore.google.com",
      "https://wallet.meteorwallet.app",
      "https://meteorwallet.app",
      "meteorwallet://",
      "meteorwalletdev://",
    ],
    external: ["meteorCom", "meteorComV2"],
  },
} as const

const connectors = new Map<NearNetwork, NearConnector>()

function getConnector(network: NearNetwork) {
  let connector = connectors.get(network)
  if (!connector) {
    const options = {
      network,
      autoConnect: false,
      footerBranding: null,
      features: { signMessage: true, signAndSendTransaction: true },
      manifest: { version: "1.1.0", wallets: [METEOR_MANIFEST] },
    } as unknown as ConstructorParameters<typeof NearConnector>[0]
    connector = new NearConnector(options)
    connectors.set(network, connector)
  }
  return connector
}

function assertActive(signal: AbortSignal) {
  signal.throwIfAborted()
}

function decodeBase64(value: string) {
  const bytes = Uint8Array.from(atob(value), (character) =>
    character.charCodeAt(0)
  )
  if (bytes.length !== 32) throw new WalletLoginError("invalidChallenge")
  return bytes
}

export class WalletLoginError extends Error {
  readonly reason: string

  constructor(reason: string) {
    super(reason)
    this.reason = reason
  }
}

export async function connectNearWallet(
  signal: AbortSignal,
  expectedAccounts?: string[],
  network: NearNetwork = NEAR_NETWORK,
  reuseConnected = false
) {
  assertActive(signal)
  const connector = getConnector(network)
  let wallet = reuseConnected ? await awaitExistingWallet(connector) : null
  assertActive(signal)
  if (!wallet) {
    try {
      wallet = await connector.connect()
    } catch (error) {
      throw normalizeNearWalletError(error)
    }
  }
  assertActive(signal)
  let accounts
  try {
    accounts = await wallet.getAccounts({ network })
  } catch (error) {
    throw normalizeNearWalletError(error)
  }
  assertActive(signal)
  const accountId = accounts[0]?.accountId
  if (!accountId) throw new WalletLoginError("noAccount")
  if (
    expectedAccounts?.length &&
    !expectedAccounts.includes(accountId)
  ) {
    throw new WalletLoginError("walletChanged")
  }
  return { connector, wallet, accountId }
}

async function awaitExistingWallet(connector: NearConnector) {
  try {
    return await connector.wallet()
  } catch {
    return null
  }
}

export async function loginWithNear({
  signal,
  onStage,
}: {
  signal: AbortSignal
  onStage: (stage: "connecting" | "signing" | "verifying") => void
}): Promise<API.WalletAuthResponse> {
  onStage("connecting")
  const { wallet, accountId } = await connectNearWallet(signal)
  assertActive(signal)
  onStage("signing")
  const challenge = await createNearWalletChallenge(
    {
      account_id: accountId,
      chain_id: NEAR_NETWORK,
      purpose: "login",
    },
    signal
  )
  assertActive(signal)
  let signed
  try {
    signed = await wallet.signMessage({
      message: challenge.message,
      recipient: challenge.recipient,
      nonce: decodeBase64(challenge.nonce),
      network: NEAR_NETWORK,
    })
  } catch (error) {
    throw normalizeNearWalletError(error)
  }
  assertActive(signal)
  if (signed.accountId !== accountId) throw new WalletLoginError("walletChanged")
  onStage("verifying")
  return verifyNearWalletSignature(
    {
      challenge_id: challenge.challenge_id,
      account_id: signed.accountId,
      public_key: signed.publicKey,
      signature: signed.signature,
    },
    signal
  )
}

export async function sendNearSubscription({
  expectedFrom,
  contractId,
  contractMethod,
  plan,
  amountAtomic,
  chainId,
  signal,
  onSubmitting,
}: {
  expectedFrom: string
  contractId: string
  contractMethod: "subscribe" | "renew"
  plan: "monthly" | "yearly"
  amountAtomic: string
  chainId: number
  signal: AbortSignal
  onSubmitting?: () => void
}) {
  const network = chainId === NEAR_CHAIN_IDS.mainnet ? "mainnet" : "testnet"
  if (NEAR_CHAIN_IDS[network] !== chainId) {
    throw new WalletLoginError("wrongNetwork")
  }
  const { wallet, accountId } = await connectNearWallet(
    signal,
    [expectedFrom],
    network,
    true
  )
  if (!/^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(contractId)) {
    throw new Error("Invalid subscription contract")
  }
  if (contractMethod !== "subscribe" && contractMethod !== "renew") {
    throw new Error("Invalid subscription method")
  }
  if (!/^\d+$/.test(amountAtomic) || BigInt(amountAtomic) <= 0n) {
    throw new Error("Invalid payment amount")
  }
  onSubmitting?.()
  let result: unknown
  try {
    result = await wallet.signAndSendTransaction({
      network,
      signerId: accountId,
      receiverId: contractId,
      actions: [
        {
          type: "FunctionCall",
          params: {
            methodName: contractMethod,
            args: { plan: plan === "monthly" ? "Monthly" : "Yearly" },
            gas: "100000000000000",
            deposit: amountAtomic,
          },
        },
      ],
    })
  } catch (error) {
    throw normalizeNearWalletError(error)
  }
  const payload = result as {
    transaction?: { hash?: unknown }
    transaction_outcome?: { id?: unknown }
  }
  const txHash =
    typeof payload?.transaction?.hash === "string"
      ? payload.transaction.hash
      : payload?.transaction_outcome?.id
  if (
    typeof txHash !== "string" ||
    !/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(txHash)
  ) {
    throw new Error("Wallet returned an invalid transaction hash")
  }
  return txHash
}

function normalizeNearWalletError(error: unknown) {
  const code = (error as { code?: number })?.code
  if (code === 4001) return new WalletLoginError("rejected")
  if (code === -32002) return new WalletLoginError("pending")
  const message = error instanceof Error ? error.message.toLowerCase() : ""
  if (message.includes("reject") || message.includes("cancel")) {
    return new WalletLoginError("rejected")
  }
  return error
}

export function nearWalletErrorReason(error: unknown): string {
  if (error instanceof WalletLoginError) return error.reason
  const walletError = error as { code?: number }
  if (walletError?.code === 4001) return "rejected"
  if (walletError?.code === -32002) return "pending"
  const apiError = error as {
    response?: { status?: number }
    data?: { detail?: { code?: string } }
  }
  switch (apiError?.data?.detail?.code) {
    case "wallet_registration_disabled":
      return "registrationDisabled"
    case "unsupported_wallet_chain":
      return "unsupportedChain"
    case "near_login_key_mismatch":
      return "keyMismatch"
  }
  switch (apiError?.response?.status) {
    case 401:
      return "expired"
    case 403:
      return "forbidden"
    case 404:
    case 503:
      return "unavailable"
    case 429:
      return "rateLimited"
    default:
      return "failed"
  }
}
