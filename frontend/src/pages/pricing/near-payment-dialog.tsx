import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { LoaderCircleIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { createPaymentOrder, getPaymentOrder } from "@/lib/service"
import {
  connectNearWallet,
  NEAR_CHAIN_IDS,
  NEAR_NETWORK,
  nearWalletErrorReason,
  sendNearSubscription,
} from "@/lib/near-wallet"
import {
  confirmNearPayment,
  formatNear,
  loadNearPayment,
  saveNearPayment,
  validateNearOrder,
} from "@/lib/near-payment"
import type { PendingNearPayment } from "@/lib/near-payment"
import { createPaymentIdempotencyKey } from "@/lib/mon-payment"

export function NearPaymentDialog({
  user,
  interval,
  open,
  onOpenChange,
  onPaid,
}: {
  user: API.User
  interval: "monthly" | "yearly"
  open: boolean
  onOpenChange: (open: boolean) => void
  onPaid: () => Promise<unknown>
}) {
  const { t } = useTranslation()
  const [payment, setPayment] = useState<PendingNearPayment | null>(() =>
    loadNearPayment(user.id)
  )
  const [stage, setStage] = useState<
    "preparing" | "sending" | "confirming" | null
  >(null)
  const [error, setError] = useState<string | null>(null)
  const [manualHash, setManualHash] = useState("")
  const attempt = useRef<AbortController | null>(null)

  useEffect(() => () => attempt.current?.abort(), [])

  const retain = (next: PendingNearPayment | null) => {
    saveNearPayment(user.id, next)
    setPayment(next)
  }

  const confirm = async (current: PendingNearPayment, signal: AbortSignal) => {
    if (!current.txHash) return
    setStage("confirming")
    const result = await confirmNearPayment(
      current.order.id,
      current.txHash,
      signal,
      (order) => retain({ ...current, order })
    )
    if (result?.status === "paid") {
      await onPaid()
    } else if (result?.status === "expired") {
      setError("paymentExpired")
    }
  }

  const run = async (action: (signal: AbortSignal) => Promise<void>) => {
    if (attempt.current) return
    const controller = new AbortController()
    attempt.current = controller
    setError(null)
    try {
      await action(controller.signal)
    } catch (cause) {
      if (!controller.signal.aborted) {
        const reason = nearWalletErrorReason(cause)
        console.error(cause)
        const message = cause instanceof Error ? cause.message : ""
        setError(
          message === "invalidNearOrder"
            ? "near.invalidOrder"
            : message === "expiredOrder"
              ? "paymentExpired"
              : message === "invalidTransaction"
                ? "invalidTransaction"
                : [
                      "rejected",
                      "pending",
                      "noAccount",
                      "walletChanged",
                      "wrongNetwork",
                    ].includes(reason)
                  ? `auth.near.errors.${reason}`
                  : "near.paymentError"
        )
      }
    } finally {
      attempt.current = null
      if (!controller.signal.aborted) setStage(null)
    }
  }

  const prepare = () =>
    run(async (signal) => {
      setStage("preparing")
      const addresses =
        user.wallets
          ?.filter(
            (wallet) =>
              wallet.namespace === "near" &&
              wallet.identity_scope === NEAR_NETWORK
          )
          .map((wallet) => wallet.address) ?? []
      if (!addresses.length) {
        setError("nearWalletRequired")
        return
      }
      const { accountId } = await connectNearWallet(
        signal,
        addresses,
        NEAR_NETWORK
      )
      const order = await createPaymentOrder({
        interval,
        payerAddress: accountId,
        idempotencyKey: createPaymentIdempotencyKey(),
        chain: "near",
        paymentMode: "contract",
      })
      signal.throwIfAborted()
      validateNearOrder(order, interval, NEAR_NETWORK)
      retain({ order })
    })

  const pay = () =>
    run(async (signal) => {
      if (!payment || payment.txHash || payment.submissionStarted) return
      setStage("sending")
      const order = await getPaymentOrder(payment.order.id, signal)
      validateNearOrder(order, payment.order.interval, NEAR_NETWORK)
      let submitted = false
      try {
        const txHash = await sendNearSubscription({
          expectedFrom: order.expected_payer_address,
          contractId: order.contract_id ?? order.recipient_address,
          contractMethod: order.contract_method ?? "subscribe",
          plan: order.interval,
          amountAtomic: order.amount_atomic,
          chainId: order.chain_id,
          signal,
          onSubmitting: () => {
            retain({ order, submissionStarted: true })
            submitted = true
          },
        })
        const current = { order, txHash, submissionStarted: true }
        retain(current)
        await confirm(current, signal)
      } catch (cause) {
        if (submitted && nearWalletErrorReason(cause) === "rejected") {
          retain({ order })
        }
        throw cause
      }
    })

  const resume = () =>
    run(async (signal) => {
      if (!payment) return
      const txHash = payment.txHash ?? manualHash.trim()
      if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(txHash)) return
      const current = { ...payment, txHash }
      retain(current)
      await confirm(current, signal)
    })

  const paid = payment?.order.status === "paid"
  const expired = payment?.order.status === "expired"
  const locked = stage === "sending" || stage === "preparing"
  const networkLabel =
    payment?.order.chain_id === NEAR_CHAIN_IDS.mainnet
      ? t("pricing.near.mainnet")
      : t("pricing.near.testnet")

  return (
    <Dialog
      modal={!locked}
      open={open}
      onOpenChange={(next) => {
        if (!locked) onOpenChange(next)
      }}
    >
      {/* The wallet picker is mounted outside this dialog and needs clicks and focus. */}
      <DialogContent
        className="sm:max-w-lg"
        showCloseButton={!locked}
        showOverlay={!locked}
      >
        <DialogHeader>
          <DialogTitle>{t("pricing.near.paymentTitle")}</DialogTitle>
          <DialogDescription>{t("pricing.near.paymentHint")}</DialogDescription>
        </DialogHeader>
        {payment ? (
          <dl className="grid gap-3 rounded-lg border p-4 text-sm">
            <div className="flex justify-between gap-4">
              <dt>{t("pricing.billingLabel")}</dt>
              <dd>{t(`pricing.billing.${payment.order.interval}`)}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt>{t("pricing.paymentAmount")}</dt>
              <dd className="font-semibold">
                {formatNear(payment.order.amount_atomic)} NEAR
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt>{t("pricing.paymentNetwork")}</dt>
              <dd>{networkLabel}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">
                {t("pricing.near.paymentAddress")}
              </dt>
              <dd className="mt-1 font-mono text-xs break-all">
                {payment.order.recipient_address}
              </dd>
            </div>
            {payment.txHash ? (
              <div>
                <dt className="text-muted-foreground">
                  {t("pricing.paymentTransaction")}
                </dt>
                <dd className="mt-1 font-mono text-xs break-all">
                  {payment.txHash}
                </dd>
              </div>
            ) : null}
          </dl>
        ) : null}
        <div aria-live="polite" className="text-sm">
          {stage ? (
            <p className="flex items-center gap-2">
              <LoaderCircleIcon className="size-4 animate-spin" />
              {t(
                stage === "sending"
                  ? "pricing.near.walletConfirmation"
                  : `pricing.paymentStages.${stage}`
              )}
            </p>
          ) : null}
          {paid ? (
            <p className="text-emerald-600">
              {t("pricing.near.paymentSuccess")}
            </p>
          ) : null}
          {!stage && payment?.txHash && !paid && !expired ? (
            <p>{t("pricing.near.paymentConfirming")}</p>
          ) : null}
          {error ? (
            <p role="alert" className="text-destructive">
              {t(error.startsWith("auth.") ? error : `pricing.${error}`)}
            </p>
          ) : null}
        </div>
        {!stage && payment?.submissionStarted && !payment.txHash && !paid ? (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">
              {t("pricing.paymentRecovery")}
            </p>
            <Input
              aria-label={t("pricing.paymentTransaction")}
              placeholder="Transaction hash"
              value={manualHash}
              onChange={(event) => setManualHash(event.target.value)}
            />
          </div>
        ) : null}
        {paid || expired ? (
          <Button
            onClick={() => {
              if (paid) retain(null)
              onOpenChange(false)
            }}
          >
            {t("pricing.paymentDone")}
          </Button>
        ) : payment?.submissionStarted || payment?.txHash ? (
          <Button
            disabled={
              !!stage ||
              (!payment.txHash &&
                !/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(manualHash.trim()))
            }
            onClick={() => void resume()}
          >
            {t("pricing.paymentCheck")}
          </Button>
        ) : (
          <Button
            disabled={!!stage}
            onClick={() => void (payment ? pay() : prepare())}
          >
            {t(payment ? "pricing.near.pay" : "pricing.near.prepare")}
          </Button>
        )}
      </DialogContent>
    </Dialog>
  )
}
