import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.decision.llm import (
    has_task_stream,
    initialize_task_stream,
    run_decision_analysis,
    stream_task_analysis,
)
from app.api.decision.market_data import service
from app.api.decision.market_data.service import Market
from app.models.auth import AuthUser
from app.services.auth import AuthService
from app.services.quota import consume_quota, get_entitlements, release_quota
from app.services.supabase_client import get_supabase_client


router = APIRouter(prefix="/decision", tags=["decision"])


class DecisionTaskRequest(BaseModel):
    market: Market = Field(
        description="Supported markets: korea, america, hongkong, coin, monad, solana, near"
    )
    code: str = Field(
        min_length=1,
        description="Stock or coin code, for example 005930, AAPL, 0700.HK, or BNB",
    )
    metadata: list["DecisionTaskMetadata"] | None = Field(
        default=None,
        description="Additional information items for the decision analysis",
    )


class DecisionTaskMetadata(BaseModel):
    label: str = Field(min_length=1)
    value: str = Field(min_length=1)


class DecisionTaskResponse(BaseModel):
    id: str
    user_id: str
    stock_code: str | None = None
    stock_name: str | None = None
    status: str | None = None
    price_snapshot: dict[str, Any] | None = None
    analysis_content: str | None = None
    operation_suggestion: list[Any] | dict[str, Any] | None = None
    created_at: str
    updated_at: str
    market: str | None = None


class DecisionTaskListResponse(BaseModel):
    id: str
    user_id: str
    stock_code: str | None = None
    stock_name: str | None = None
    status: str | None = None
    price_snapshot: dict[str, Any] | None = None
    operation_suggestion: list[Any] | dict[str, Any] | None = None
    created_at: str
    updated_at: str
    market: str | None = None


class DecisionTaskDetailResponse(DecisionTaskListResponse):
    analysis_content: str | None = None


@router.get("/report")
def get_report(
    ticker: str = Query(
        min_length=1,
        description="Stock or coin ticker, for example 005930, AAPL, 0700.HK, or BNB",
    ),
    market: Market = Query(
        description="Supported markets: korea, america, hongkong, coin, monad, solana, near"
    ),
) -> dict[str, Any]:
    result = service.get_report(market, _normalize_market_code(market, ticker))
    if not result.get("success") and result.get("error_reasoning", "").startswith(
        "Unsupported market:"
    ):
        raise HTTPException(status_code=400, detail=result["error_reasoning"])
    return result


@router.post("/tasks", response_model=DecisionTaskResponse, operation_id="createDecisionTask")
async def create_decision_task(
    request: DecisionTaskRequest,
    authorization: str | None = Header(default=None),
) -> DecisionTaskResponse:
    user = _current_user(authorization)
    market_code = _normalize_market_code(request.market, request.code)
    snapshot_result = service.fetch_compact_quote_snapshot(request.market, market_code)

    if not snapshot_result.get("success") and str(
        snapshot_result.get("error_reasoning") or ""
    ).startswith("Unsupported market:"):
        raise HTTPException(status_code=400, detail=snapshot_result["error_reasoning"])

    stock_name, price_snapshot = _extract_compact_quote_snapshot(snapshot_result)
    consume_quota(user.id, "analysis")
    try:
        task = _save_decision_task(user, request, stock_name, price_snapshot)
    except Exception:
        release_quota(user.id, "analysis")
        raise
    await initialize_task_stream(task.id)

    asyncio.create_task(
        _run_decision_analysis_with_report(
            task.id,
            request.market,
            market_code,
            request.metadata,
        )
    )
    return task


@router.get("/tasks", response_model=list[DecisionTaskListResponse], operation_id="listDecisionTasks")
def list_decision_tasks(
    authorization: str | None = Header(default=None),
) -> list[DecisionTaskListResponse]:
    user = _current_user(authorization)
    return _list_decision_tasks(user)


@router.get("/tasks/{task_id}/stream", operation_id="streamDecisionTaskAnalysis")
async def stream_decision_task_analysis(
    task_id: str,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    user = _current_user(authorization)
    _get_decision_task(user, task_id)
    return _decision_task_stream_response(task_id)


@router.get("/tasks/stream", operation_id="streamDecisionTaskAnalysisByQuery")
async def stream_decision_task_analysis_by_query(
    task_id: str = Query(alias="taskId", min_length=1),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    user = _current_user(authorization)
    _get_decision_task(user, task_id)
    return _decision_task_stream_response(task_id)


@router.get("/tasks/{task_id}", response_model=DecisionTaskDetailResponse, operation_id="getDecisionTask")
async def get_decision_task(
    task_id: str,
    authorization: str | None = Header(default=None),
) -> DecisionTaskDetailResponse:
    user = _current_user(authorization)
    task = _get_decision_task(user, task_id)
    await _restart_decision_analysis_if_interrupted(task)
    return task


def _decision_task_stream_response(task_id: str) -> StreamingResponse:
    return StreamingResponse(
        stream_task_analysis(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _run_decision_analysis_with_report(
    task_id: str,
    market: str,
    ticker: str,
    metadata: list[DecisionTaskMetadata] | None = None,
) -> None:
    report_result = await asyncio.to_thread(service.get_report, market, ticker)
    report_markdown = str(report_result.get("report") or "")
    await run_decision_analysis(
        task_id,
        report_markdown,
        market,
        [item.model_dump() for item in metadata] if metadata else None,
    )


async def _restart_decision_analysis_if_interrupted(
    task: DecisionTaskDetailResponse,
) -> None:
    if task.status != "processing":
        return
    if await has_task_stream(task.id):
        return
    if not task.market or not task.stock_code:
        return

    await initialize_task_stream(task.id)
    asyncio.create_task(
        _run_decision_analysis_with_report(task.id, task.market, task.stock_code)
    )


def _current_user(authorization: str | None) -> AuthUser:
    token = _get_bearer_token(authorization)
    return AuthService().current_user(token)


def _normalize_market_code(market: Market, code: str) -> str:
    if market not in {"coin", "monad", "solana", "near"}:
        return code
    parts = code.strip().upper().split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(
            status_code=422,
            detail="Cryptocurrency code must use EXCHANGE:SYMBOL format",
        )
    return f"{parts[0]}:{parts[1]}"


def _get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header is required")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Bearer token is required")

    return token.strip()


def _list_decision_tasks(user: AuthUser) -> list[DecisionTaskListResponse]:
    entitlements = get_entitlements(user.id)
    try:
        query = (
            get_supabase_client()
            .table("decision_tasks")
            .select(
                "id,user_id,stock_code,stock_name,status,price_snapshot,"
                "operation_suggestion,created_at,updated_at,market"
            )
            .eq("user_id", user.id)
        )
        if not entitlements.get("is_pro"):
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            query = query.gte("created_at", cutoff.isoformat())
        response = query.order("created_at", desc=True).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Decision task list fetch failed") from exc

    return [DecisionTaskListResponse(**row) for row in response.data or []]


def _get_decision_task(user: AuthUser, task_id: str) -> DecisionTaskDetailResponse:
    entitlements = get_entitlements(user.id)
    try:
        query = (
            get_supabase_client()
            .table("decision_tasks")
            .select(
                "id,user_id,stock_code,stock_name,status,price_snapshot,"
                "operation_suggestion,created_at,updated_at,market,analysis_content"
            )
            .eq("id", task_id)
            .eq("user_id", user.id)
        )
        if not entitlements.get("is_pro"):
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            query = query.gte("created_at", cutoff.isoformat())
        response = query.limit(1).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Decision task fetch failed") from exc

    rows = response.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Decision task not found")
    return DecisionTaskDetailResponse(**rows[0])


def _extract_compact_quote_snapshot(
    snapshot: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    if not snapshot.get("success"):
        detail = snapshot.get("error_reasoning")
        raise HTTPException(
            status_code=502,
            detail=detail or "Failed to fetch price snapshot",
        )

    price_snapshot = {
        key: value
        for key, value in snapshot.items()
        if key not in {"success", "error_reasoning"}
    }
    stock_name = price_snapshot.get("name")
    return str(stock_name) if stock_name else None, price_snapshot


def _save_decision_task(
    user: AuthUser,
    request: DecisionTaskRequest,
    stock_name: str | None,
    price_snapshot: dict[str, Any],
) -> DecisionTaskResponse:
    payload = {
        "user_id": user.id,
        "stock_code": request.code,
        "stock_name": stock_name,
        "status": "processing",
        "price_snapshot": price_snapshot,
        # "analysis_content": None,
        "operation_suggestion": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "market": request.market,
    }

    try:
        response = get_supabase_client().table("decision_tasks").insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Decision task creation failed") from exc

    rows = response.data or []
    if not rows:
        raise HTTPException(status_code=500, detail="Decision task creation failed")
    return DecisionTaskResponse(**rows[0])
