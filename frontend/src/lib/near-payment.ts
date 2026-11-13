import { getPaymentOrder, verifyPaymentOrder } from "./service"
import { NEAR_CHAIN_IDS } from "./near-wallet"

export type PendingNearPayment = {
  order: API.PaymentOrder
  txHash?: string
  submissionStarted?: boolean
}

const NEAR_ACCOUNT_RE = /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/
const NEAR_TX_HASH_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/

export function formatNear(amount: string, decimals = 24) {
  const digits = BigInt(amount)
    .toString()
    .padStart(decimals + 1, "0")
  const fraction = digits.slice(-decimals).replace(/0+$/, "")
  return `${digits.slice(0, -decimals)}${fraction ? `.${fraction}` : ""}`
}

export function validateNearOrder(
  order: API.PaymentOrder,
  interval: "monthly" | "yearly",
  network: "mainnet" | "testnet"
) {
  if (
    order.provider !== "crypto" ||
    (order.plan !== "pro" && order.plan !== interval) ||
    order.interval !== interval ||
    order.method !== "near_contract" ||
    order.chain_namespace !== "near" ||
    order.payment_mode !== "contract" ||
    order.asset_symbol !== "NEAR" ||
    order.amount_decimals !== 24 ||
    order.chain_id !== NEAR_CHAIN_IDS[network] ||
    !NEAR_ACCOUNT_RE.test(order.recipient_address) ||
    !NEAR_ACCOUNT_RE.test(order.expected_payer_address) ||
    !NEAR_ACCOUNT_RE.test(order.contract_id ?? "") ||
    order.contract_id !== order.recipient_address ||
    !["subscribe", "renew"].includes(order.contract_method ?? "") ||
    !/^\d+$/.test(order.amount_atomic) ||
    BigInt(order.amount_atomic) <= 0n
  ) {
    throw new Error("invalidNearOrder")
  }
  if (
    order.status !== "created" ||
    !Number.isFinite(Date.parse(order.expires_at)) ||
    Date.parse(order.expires_at) <= Date.now()
  ) {
    throw new Error("expiredOrder")
  }
}

const storageKey = (userId: string) => `easystock.near-payment.${userId}`

export function loadNearPayment(userId: string): PendingNearPayment | null {
  try {
    const stored = JSON.parse(
      localStorage.getItem(storageKey(userId)) ?? "null"
    )
    return stored?.order?.id ? (stored as PendingNearPayment) : null
  } catch {
    return null
  }
}

export function saveNearPayment(
  userId: string,
  payment: PendingNearPayment | null
) {
  try {
    if (payment) {
      localStorage.setItem(storageKey(userId), JSON.stringify(payment))
    } else {
      localStorage.removeItem(storageKey(userId))
    }
  } catch {
    // The server order remains recoverable if local storage is unavailable.
  }
}

function delay(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted()
    const abort = () => {
      clearTimeout(timer)
      reject(signal.reason)
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort)
      resolve()
    }, 3000)
    signal.addEventListener("abort", abort, { once: true })
  })
}

export async function confirmNearPayment(
  orderId: string,
  txHash: string,
  signal: AbortSignal,
  onUpdate: (order: API.PaymentOrder) => void
) {
  if (!NEAR_TX_HASH_RE.test(txHash)) throw new Error("invalidTransaction")
  for (let attempt = 0; attempt < 60; attempt++) {
    signal.throwIfAborted()
    const order = await getPaymentOrder(orderId, signal)
    onUpdate(order)
    if (order.status === "paid" || order.status === "expired") return order
    const verified = await verifyPaymentOrder(orderId, txHash)
    onUpdate(verified)
    if (verified.status === "paid" || verified.status === "expired") {
      return verified
    }
    await delay(signal)
  }
  return null
}
