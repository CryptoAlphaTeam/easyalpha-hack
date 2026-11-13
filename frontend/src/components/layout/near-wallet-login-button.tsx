import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { LoaderCircleIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import useUser from "@/hooks/use-user"
import {
  loginWithNear,
  nearWalletErrorReason,
} from "@/lib/near-wallet"

export function NearWalletLoginButton() {
  const { t } = useTranslation()
  const { completeSignIn, signInModalOpen } = useUser()
  const [stage, setStage] = useState<
    "connecting" | "signing" | "verifying" | null
  >(null)
  const [error, setError] = useState<string | null>(null)
  const attempt = useRef<AbortController | null>(null)

  useEffect(() => () => attempt.current?.abort(), [])

  useEffect(() => {
    if (!signInModalOpen) attempt.current?.abort()
  }, [signInModalOpen])

  const connect = async () => {
    if (attempt.current) return
    const controller = new AbortController()
    attempt.current = controller
    setError(null)
    try {
      const session = await loginWithNear({
        signal: controller.signal,
        onStage: setStage,
      })
      if (!controller.signal.aborted) await completeSignIn(session)
    } catch (cause) {
      if (!controller.signal.aborted) setError(nearWalletErrorReason(cause))
    } finally {
      attempt.current = null
      if (!controller.signal.aborted) setStage(null)
    }
  }

  return (
    <div className="grid gap-2">
      <Button
        type="button"
        className="w-full"
        variant="outline"
        size="lg"
        disabled={stage !== null}
        aria-busy={stage !== null}
        onClick={() => void connect()}
      >
        {stage ? (
          <LoaderCircleIcon className="size-4 animate-spin" />
        ) : (
          <span className="flex size-4 items-center justify-center rounded-full bg-[#111] text-[10px] font-bold text-white">
            N
          </span>
        )}
        <span>
          {stage ? t(`auth.near.${stage}`) : t("auth.continueWithNear")}
        </span>
      </Button>
      <p
        className="text-center text-xs text-muted-foreground"
        aria-live="polite"
      >
        {t("auth.near.hint")}
      </p>
      {error ? (
        <p role="alert" className="text-center text-xs text-destructive">
          {t(`auth.near.errors.${error}`)}
        </p>
      ) : null}
    </div>
  )
}
