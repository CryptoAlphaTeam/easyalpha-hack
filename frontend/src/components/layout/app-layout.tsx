import { Link, Outlet, useLocation } from "react-router"
import { useTranslation } from "react-i18next"

import { AppSidebar } from "@/components/layout/app-sidebar"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog.tsx"
import useUser from "@/hooks/use-user.ts"
import { NearWalletLoginButton } from "@/components/layout/near-wallet-login-button"

const routeLabels: Record<string, string> = {
  "/": "routes.stockScreener",
  "/decision": "routes.decisionRecords",
  "/stock-screener": "routes.stockScreener",
  "/pricing": "routes.pricing",
  "/chat": "routes.chat",
  "/alpha/research": "routes.factorResearch",
  "/alpha/library": "routes.factorLibrary",
}

export function AppLayout() {
  const location = useLocation()
  const { t } = useTranslation()
  const { signInModalOpen, setSignInModalOpen } = useUser()
  const isChatRoute =
    location.pathname === "/chat" || location.pathname.startsWith("/chat/")
  const isAlphaRoute = location.pathname.startsWith("/alpha")
  const isDecisionDetail = location.pathname.startsWith("/decision/")
  const currentLabel =
    routeLabels[location.pathname] ??
    (isChatRoute
      ? "routes.chat"
      : isAlphaRoute
        ? location.pathname.startsWith("/alpha/library")
          ? "routes.factorLibrary"
          : "routes.factorResearch"
        : isDecisionDetail
          ? "routes.decisionRecords"
          : "routes.stockScreener")

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset
        className={`min-w-0 md:[--app-header-left:var(--sidebar-width)] md:peer-data-[state=collapsed]:[--app-header-left:var(--sidebar-width-icon)]${
          isChatRoute || isAlphaRoute
            ? "h-svh max-h-svh min-h-0 overflow-hidden"
            : ""
        }`}
      >
        <header className="fixed inset-x-0 top-0 z-20 flex h-14 shrink-0 items-center gap-2 border-b bg-background px-4 md:left-(--app-header-left)">
          <SidebarTrigger />
          <Breadcrumb>
            <BreadcrumbList>
              <BreadcrumbItem className="hidden sm:inline-flex">
                <BreadcrumbLink asChild>
                  <Link to="/">{t("routes.home")}</Link>
                </BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator className="hidden sm:inline-flex" />
              <BreadcrumbItem>
                {isDecisionDetail ? (
                  <BreadcrumbLink asChild>
                    <Link to="/decision">{t(currentLabel)}</Link>
                  </BreadcrumbLink>
                ) : (
                  <BreadcrumbPage>{t(currentLabel)}</BreadcrumbPage>
                )}
              </BreadcrumbItem>
              {isDecisionDetail ? (
                <>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    <BreadcrumbPage>
                      {t("routes.decisionDetail")}
                    </BreadcrumbPage>
                  </BreadcrumbItem>
                </>
              ) : null}
            </BreadcrumbList>
          </Breadcrumb>
        </header>
        <div
          className={`flex flex-1 flex-col px-4 pt-20 pb-6 sm:px-6 lg:px-8${
            isChatRoute || isAlphaRoute ? "min-h-0 overflow-hidden pb-0" : ""
          }`}
        >
          <Outlet />
        </div>
        <footer
          className={`shrink-0 px-4 pb-4 text-center text-sm text-slate-500 sm:px-6 lg:px-8${
            isChatRoute || isAlphaRoute ? "hidden" : ""
          }`}
        >
          <nav aria-label="Legal">
            <a href="/privacy-policy/" className="text-inherit">
              Privacy Policy
            </a>
            <span aria-hidden="true" className="mx-3">
              ·
            </span>
            <a href="/terms-of-service/" className="text-inherit">
              Terms of Service
            </a>
          </nav>
        </footer>
      </SidebarInset>
      <Dialog
        modal={false}
        open={signInModalOpen}
        onOpenChange={setSignInModalOpen}
      >
        <DialogContent showOverlay={false}>
          <DialogHeader>
            <DialogTitle>{t("auth.login")}</DialogTitle>
          </DialogHeader>
          <NearWalletLoginButton />
        </DialogContent>
      </Dialog>
    </SidebarProvider>
  )
}
