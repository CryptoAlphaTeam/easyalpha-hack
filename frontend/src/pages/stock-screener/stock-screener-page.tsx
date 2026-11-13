import * as React from "react"
import {
  type Column,
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  type SortingState,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table"
import {
  useCreation,
  useRequest,
  useMemoizedFn,
  useLocalStorageState,
} from "ahooks"
import {
  ArrowDownWideNarrowIcon,
  ArrowUpDownIcon,
  ArrowUpNarrowWideIcon,
  BotIcon,
  ChevronDownIcon,
  SearchIcon,
  Share2Icon,
} from "lucide-react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Carousel,
  CarouselContent,
  CarouselItem,
  CarouselNext,
  CarouselPrevious,
} from "@/components/ui/carousel"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { FlickeringGrid } from "@/components/ui/flickering-grid"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { RainbowButton } from "@/components/ui/rainbow-button"
import { Spinner } from "@/components/ui/spinner"
import {
  getDefaultStockScreener,
  getStockScreenerResult,
  queryStockScreener,
  getQuotaError,
} from "@/lib/service"
import {
  DEFAULT_STOCK_SCREENER_MARKET,
  getCountryLogoUrl,
  stockScreenerMarketOptions,
} from "@/lib/markets"
import {
  cn,
  getScreenerRowCode,
  getScreenerRowCodeAndDescription,
  getScreenerRowName,
  getStockQuoteUrl,
} from "@/lib/utils"
import useUser from "@/hooks/use-user.ts"
import useBilling from "@/hooks/use-billing"
import useDecisionTasks from "@/hooks/use-decision-tasks.ts"
import { useNavigate } from "react-router"

export function StockScreenerPage() {
  const { i18n, t } = useTranslation()
  const navigate = useNavigate()
  const { isAuthenticated, setSignInModalOpen } = useUser()
  const { refreshBilling } = useBilling()
  const { createDecisionTask, createDecisionTaskLoading, creatingStockCode } =
    useDecisionTasks()

  const [market, setMarket] = useLocalStorageState<API.StockScreenerMarket>(
    "stock-screener-market",
    {
      defaultValue: getDefaultStockScreenerMarket(),
    }
  )
  React.useEffect(() => {
    if (!stockScreenerMarketOptions.some((option) => option.code === market)) {
      setMarket(DEFAULT_STOCK_SCREENER_MARKET)
    }
  }, [market, setMarket])
  const [query, setQuery] = React.useState(getStoredStockScreenerQuery)
  const [loadingMessageIndex, setLoadingMessageIndex] = React.useState(0)
  const [summary, setSummary] = React.useState<string>("")
  const [rows, setRows] = React.useState<API.ScreenerResultRow[]>([])
  const hasSubmittedQueryRef = React.useRef(false)
  const selectedMarket =
    stockScreenerMarketOptions.find((option) => option.code === market) ??
    stockScreenerMarketOptions[0]
  const exampleKeys = marketExampleKeys[market] ?? stockExampleKeys

  const sortableHeader = useMemoizedFn(
    (
      title: React.ReactNode,
      column: Column<API.ScreenerResultRow, unknown>
    ) => {
      const isSorted = column.getIsSorted()
      return (
        <Button
          variant="ghost"
          onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
        >
          {title}
          {isSorted === "asc" ? (
            <ArrowUpNarrowWideIcon />
          ) : isSorted === "desc" ? (
            <ArrowDownWideNarrowIcon />
          ) : (
            <ArrowUpDownIcon className="opacity-40" />
          )}
        </Button>
      )
    }
  )

  const [sorting, setSorting] = React.useState<SortingState>([])

  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    state: {
      sorting,
    },
    onSortingChange: setSorting,
    getSortedRowModel: getSortedRowModel(),
  })

  const { loading, run } = useRequest(
    async (nextQuery: string) =>
      queryStockScreener({
        market,
        query: nextQuery,
      }),
    {
      manual: true,
      onSuccess: ({ metadata, id }) => {
        const url = new URL(window.location.href)
        url.searchParams.set("screen_id", id)
        window.history.replaceState({}, "", url)

        setSummary(metadata.summary)
        setRows(metadata.results)
        void refreshBilling().catch(() => undefined)
      },
      onError: (error) => {
        toast.error(
          getQuotaError(error)
            ? t("pricing.quotaReached")
            : t("stockScreener.error")
        )
      },
    }
  )

  useRequest(
    async () => {
      const urlParams = new URLSearchParams(window.location.search)
      const screenId = urlParams.get("screen_id")
      if (screenId) {
        const {
          metadata,
          market: resultMarket,
          query,
        } = await getStockScreenerResult({
          screenId: screenId,
        })
        if (!hasSubmittedQueryRef.current) {
          setQuery(query)
          if (
            stockScreenerMarketOptions.some(
              (option) => option.code === resultMarket
            )
          ) {
            setMarket(resultMarket as API.StockScreenerMarket)
          }
          setSummary(metadata.summary)
          setRows(metadata.results)
        }
      } else {
        const result = await getDefaultStockScreener({
          market,
        })
        if (!hasSubmittedQueryRef.current) {
          setRows(result)
        }
      }
    },
    {
      refreshDeps: [market],
      onError: () => {
        toast.error(t("stockScreener.error"))
      },
    }
  )

  React.useEffect(() => {
    if (!loading) {
      setLoadingMessageIndex(0)
      return
    }

    const intervalId = window.setInterval(() => {
      setLoadingMessageIndex((index) => (index + 1) % loadingMessageKeys.length)
    }, 3000)

    return () => window.clearInterval(intervalId)
  }, [loading])

  const canSubmit = query.trim().length > 0 && !loading
  const loadingMessageKey = loadingMessageKeys[loadingMessageIndex]

  const handleExampleSelect = useMemoizedFn((example: ExampleKey) => {
    setQuery(t(`stockScreener.examples.items.${example}.description`))
  })

  const handleMarketChange = useMemoizedFn((nextMarket: string) => {
    if (
      !stockScreenerMarketOptions.some((option) => option.code === nextMarket)
    ) {
      return
    }

    const url = new URL(window.location.href)
    url.searchParams.delete("screen_id")
    window.history.replaceState(window.history.state, "", url)

    setSummary("")
    setRows([])
    setMarket(nextMarket as API.StockScreenerMarket)
    hasSubmittedQueryRef.current = false
  })

  const handleSubmit = useMemoizedFn(
    (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault()

      if (!canSubmit) {
        return
      }

      const nextQuery = query.trim()

      saveStockScreenerQuery(nextQuery)

      if (!isAuthenticated) {
        setSignInModalOpen(true)
        return
      }

      hasSubmittedQueryRef.current = true

      run(nextQuery)
    }
  )

  const handleShareClick = useMemoizedFn(async () => {
    await navigator.clipboard.writeText(window.location.href)
    toast.success(t("stockScreener.shareCopied"))
  })

  return (
    <section className="w-full max-w-full min-w-0">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">
          {t("stockScreener.title")}
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {t("stockScreener.description")}
        </p>
      </div>

      <form className="mb-3 space-y-3" onSubmit={handleSubmit}>
        <div className="space-y-2">
          <Carousel
            opts={{
              align: "start",
              slidesToScroll: 2,
            }}
          >
            <div className="mb-2 flex items-center justify-between gap-3">
              <div className="text-sm font-medium">
                {t("stockScreener.examples.title")}
              </div>
              <div className="flex items-center gap-1">
                <CarouselPrevious
                  aria-label={t("stockScreener.examples.previous")}
                  className="static inset-auto my-0"
                  type="button"
                />
                <CarouselNext
                  aria-label={t("stockScreener.examples.next")}
                  className="static inset-auto my-0"
                  type="button"
                />
              </div>
            </div>
            <CarouselContent className="-ml-3">
              {exampleKeys.map((example) => (
                <CarouselItem
                  key={example}
                  className="basis-1/2 pl-3 sm:basis-1/3 md:basis-1/3 lg:basis-1/3 xl:basis-1/4 2xl:basis-1/5"
                >
                  <div className="p-0.5">
                    <Card
                      aria-label={t(
                        `stockScreener.examples.items.${example}.title`
                      )}
                      className="h-full cursor-pointer transition-colors hover:bg-accent/50 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                      onClick={() => handleExampleSelect(example)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault()
                          handleExampleSelect(example)
                        }
                      }}
                      role="button"
                      size="sm"
                      tabIndex={0}
                    >
                      <CardHeader>
                        <CardTitle>
                          {t(`stockScreener.examples.items.${example}.title`)}
                        </CardTitle>
                      </CardHeader>
                      <CardContent className="line-clamp-2 min-h-10 text-muted-foreground">
                        {t(
                          `stockScreener.examples.items.${example}.description`
                        )}
                      </CardContent>
                    </Card>
                  </div>
                </CarouselItem>
              ))}
            </CarouselContent>
          </Carousel>
        </div>
        <Textarea
          className="min-h-32 resize-y bg-background"
          placeholder={t("stockScreener.placeholder")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="flex items-center justify-between gap-3">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button type="button" variant="outline">
                <img
                  alt=""
                  className="size-4 rounded-full"
                  src={getMarketLogoUrl(
                    selectedMarket.countryCode,
                    selectedMarket.code
                  )}
                />
                {t(selectedMarket.labelKey)}
                <ChevronDownIcon />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuRadioGroup
                value={market}
                onValueChange={handleMarketChange}
              >
                {stockScreenerMarketOptions.map((option) => (
                  <DropdownMenuRadioItem key={option.code} value={option.code}>
                    <img
                      alt=""
                      className="size-4 rounded-full"
                      src={getMarketLogoUrl(option.countryCode, option.code)}
                    />
                    {t(option.labelKey)}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <Button disabled={!canSubmit} type="submit">
            <SearchIcon />
            {loading ? t("stockScreener.loading") : t("stockScreener.submit")}
          </Button>
        </div>
      </form>

      {summary && (
        <>
          <div className="mb-2 rounded-md border bg-card p-4 text-sm text-card-foreground">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <Marker>
                <MarkerIcon>
                  <BotIcon />
                </MarkerIcon>
                <MarkerContent>{t("stockScreener.summary")}</MarkerContent>
              </Marker>
            </div>
            <div className="mt-2">{summary}</div>
          </div>
          <div className="mb-2">
            <Button
              aria-label={t("stockScreener.share")}
              size="sm"
              type="button"
              variant="outline"
              onClick={handleShareClick}
            >
              <Share2Icon />
              {t("stockScreener.share")}
            </Button>
          </div>
        </>
      )}

      {loading ? (
        <div className="relative h-72 overflow-hidden rounded-md border bg-card">
          <FlickeringGrid
            className="absolute inset-0 opacity-80"
            squareSize={4}
            gridGap={6}
            color="#60A5FA"
            maxOpacity={0.5}
            flickerChance={0.1}
          />
          <div className="absolute inset-0 flex items-center justify-center bg-card/20">
            <Marker
              key={`${i18n.language}-${loadingMessageKey}`}
              className="animate-screener-loading-message w-auto text-foreground"
              variant="separator"
            >
              <MarkerIcon>{loadingMessageIcon[loadingMessageIndex]}</MarkerIcon>
              <MarkerContent>
                {t(`stockScreener.loadingMessages.${loadingMessageKey}`)}
              </MarkerContent>
            </Marker>
          </div>
        </div>
      ) : (
        <div className="max-w-full min-w-0 overflow-hidden rounded-md border bg-card">
          <Table>
            <TableHeader>
              {table.getHeaderGroups().map((headerGroup) => (
                <TableRow key={headerGroup.id}>
                  {headerGroup.headers.map((header) => (
                    <TableHead
                      className={cn(
                        header.column.id === "company" &&
                          "sticky left-0 z-10 bg-card shadow-[4px_0_4px_-4px] shadow-border"
                      )}
                      key={header.id}
                      onClick={header.column.getToggleSortingHandler()}
                      style={{ cursor: "pointer" }}
                    >
                      {header.isPlaceholder
                        ? null
                        : flexRender(
                            header.column.columnDef.header,
                            header.getContext()
                          )}
                    </TableHead>
                  ))}
                </TableRow>
              ))}
            </TableHeader>
            <TableBody>
              {table.getRowModel().rows.length > 0 ? (
                table.getRowModel().rows.map((row) => (
                  <TableRow key={row.id}>
                    {row.getVisibleCells().map((cell) => (
                      <TableCell
                        className={cn(
                          cell.column.id === "company" &&
                            "sticky left-0 z-[1] bg-card shadow-[4px_0_4px_-4px] shadow-border"
                        )}
                        key={cell.id}
                      >
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext()
                        )}
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell
                    className="h-28 text-center text-muted-foreground"
                    colSpan={columns.length}
                  >
                    {query.trim().length > 0
                      ? t("stockScreener.noMatches")
                      : t("stockScreener.empty")}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </section>
  )
}
