from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.alpha import store
from app.alpha.events import apply_event
from app.alpha.worker import launch_run
from app.alpha_redis import get_redis, read_events, replay_events, stream_key
from app.core.config import get_settings
from app.models.alpha import (
    ACTIVE_RUN_STATUSES,
    AlphaChat,
    AlphaChatMessage,
    AlphaChatRun,
    ChatAccepted,
    ChatHistory,
    ChatMessagePublic,
    ChatPublic,
    ChatRunPublic,
    SendMessageBody,
)
from app.services.alpha_db import alpha_session
from app.services.auth import AuthService
from app.services.quota import consume_quota, release_quota

router = APIRouter(prefix="/alpha", tags=["alpha"])


def current_user_id(authorization: str | None = Header(default=None)) -> uuid.UUID:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "Bearer token is required")
    try:
        return uuid.UUID(AuthService().current_user(token.strip()).id)
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(401, "Invalid user identity") from exc


def _latest_run(db, chat_id: uuid.UUID) -> AlphaChatRun | None:
    return db.execute(
        select(AlphaChatRun)
        .where(AlphaChatRun.chat_id == chat_id)
        .order_by(AlphaChatRun.turn_no.desc())
        .limit(1)
    ).scalar_one_or_none()


def _owned_chat(db, user_id: uuid.UUID, chat_id: uuid.UUID, *, lock: bool = False) -> AlphaChat:
    query = select(AlphaChat).where(
        AlphaChat.id == chat_id,
        AlphaChat.user_id == user_id,
        AlphaChat.is_deleted.is_(False),
    )
    if lock:
        query = query.with_for_update()
    chat = db.execute(query).scalar_one_or_none()
    if chat is None:
        raise HTTPException(404, "chat_not_found")
    return chat


def _chat_public(db, chat: AlphaChat) -> ChatPublic:
    latest = _latest_run(db, chat.id)
    return ChatPublic(
        id=chat.id,
        user_id=chat.user_id,
        title=chat.title,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        active_run_id=latest.id if latest and latest.status in ACTIVE_RUN_STATUSES else None,
        last_run_status=latest.status if latest else None,
    )


def _accepted(db, chat: AlphaChat, run: AlphaChatRun) -> ChatAccepted:
    message = db.execute(
        select(AlphaChatMessage).where(
            AlphaChatMessage.run_id == run.id,
            AlphaChatMessage.type == "user",
        )
    ).scalar_one()
    return ChatAccepted(
        chat=_chat_public(db, chat),
        run=ChatRunPublic.model_validate(run),
        user_message=ChatMessagePublic.model_validate(message),
    )


def _sse(kind: str, data: dict, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chats", response_model=ChatAccepted, status_code=202)
async def create_chat(body: SendMessageBody, user_id: uuid.UUID = Depends(current_user_id)) -> Any:
    with alpha_session() as db:
        existing = db.execute(
            select(AlphaChat).where(
                AlphaChat.user_id == user_id,
                AlphaChat.create_request_id == body.request_id,
            )
        ).scalar_one_or_none()
        if existing:
            if existing.is_deleted:
                raise HTTPException(409, "chat_deleted")
            run = db.execute(
                select(AlphaChatRun).where(
                    AlphaChatRun.chat_id == existing.id,
                    AlphaChatRun.turn_no == 1,
                )
            ).scalar_one()
            response = _accepted(db, existing, run)
            if run.status == "queued":
                launch_run(run.id, str(user_id))
            return response

    consume_quota(str(user_id), "alpha")
    try:
        now = store._now()
        with alpha_session() as db:
            existing = db.execute(
                select(AlphaChat).where(
                    AlphaChat.user_id == user_id,
                    AlphaChat.create_request_id == body.request_id,
                )
            ).scalar_one_or_none()
            if existing:
                run = _latest_run(db, existing.id)
                if run is None:
                    raise HTTPException(500, "chat_inconsistent")
                response = _accepted(db, existing, run)
                release_quota(str(user_id), "alpha")
            else:
                chat = AlphaChat(
                    user_id=user_id,
                    title=body.content[:20],
                    sdk_session_id=str(uuid.uuid4()),
                    create_request_id=body.request_id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(chat)
                db.flush()
                run = AlphaChatRun(
                    chat_id=chat.id,
                    request_id=body.request_id,
                    turn_no=1,
                    created_at=now,
                    updated_at=now,
                )
                db.add(run)
                db.flush()
                db.add(
                    AlphaChatMessage(
                        run_id=run.id,
                        seq=0,
                        type="user",
                        content=body.content,
                        source_item_id=str(body.request_id),
                        status="completed",
                        revision=1,
                        created_at=now,
                        updated_at=now,
                        finished_at=now,
                    )
                )
                db.commit()
                response = _accepted(db, chat, run)
        launch_run(run.id, str(user_id))
        return response
    except Exception:
        release_quota(str(user_id), "alpha")
        raise


@router.post("/chats/{chat_id}/messages", response_model=ChatAccepted, status_code=202)
async def send_message(
    chat_id: uuid.UUID,
    body: SendMessageBody,
    user_id: uuid.UUID = Depends(current_user_id),
) -> Any:
    with alpha_session() as db:
        chat = _owned_chat(db, user_id, chat_id, lock=True)
        existing = db.execute(
            select(AlphaChatRun).where(
                AlphaChatRun.chat_id == chat_id,
                AlphaChatRun.request_id == body.request_id,
            )
        ).scalar_one_or_none()
        if existing:
            response = _accepted(db, chat, existing)
            if existing.status == "queued":
                launch_run(existing.id, str(user_id))
            return response
        previous = _latest_run(db, chat_id)
        if previous and previous.status in ACTIVE_RUN_STATUSES:
            raise HTTPException(409, "chat_busy")

    consume_quota(str(user_id), "alpha")
    try:
        now = store._now()
        with alpha_session() as db:
            chat = _owned_chat(db, user_id, chat_id, lock=True)
            previous = _latest_run(db, chat_id)
            if previous and previous.status in ACTIVE_RUN_STATUSES:
                raise HTTPException(409, "chat_busy")
            existing = db.execute(
                select(AlphaChatRun).where(
                    AlphaChatRun.chat_id == chat_id,
                    AlphaChatRun.request_id == body.request_id,
                )
            ).scalar_one_or_none()
            if existing:
                response = _accepted(db, chat, existing)
                run = existing
                release_quota(str(user_id), "alpha")
            else:
                run = AlphaChatRun(
                    chat_id=chat_id,
                    request_id=body.request_id,
                    turn_no=(previous.turn_no + 1 if previous else 1),
                    created_at=now,
                    updated_at=now,
                )
                db.add(run)
                db.flush()
                db.add(
                    AlphaChatMessage(
                        run_id=run.id,
                        seq=0,
                        type="user",
                        content=body.content,
                        source_item_id=str(body.request_id),
                        status="completed",
                        created_at=now,
                        updated_at=now,
                        finished_at=now,
                    )
                )
                chat.updated_at = now
                db.commit()
                response = _accepted(db, chat, run)
        launch_run(run.id, str(user_id))
        return response
    except Exception:
        release_quota(str(user_id), "alpha")
        raise


@router.get("/chats", response_model=list[ChatPublic])
def list_chats(
    limit: int = Query(100, ge=1, le=200),
    user_id: uuid.UUID = Depends(current_user_id),
) -> list[ChatPublic]:
    with alpha_session() as db:
        chats = db.execute(
            select(AlphaChat)
            .where(AlphaChat.user_id == user_id, AlphaChat.is_deleted.is_(False))
            .order_by(AlphaChat.updated_at.desc(), AlphaChat.id.desc())
            .limit(limit)
        ).scalars().all()
        return [_chat_public(db, chat) for chat in chats]


@router.get("/chats/{chat_id}/messages", response_model=ChatHistory)
def get_chat_messages(
    chat_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    before_turn: int | None = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=100),
) -> ChatHistory:
    with alpha_session() as db:
        _owned_chat(db, user_id, chat_id)
        query = select(AlphaChatRun).where(AlphaChatRun.chat_id == chat_id)
        if before_turn is not None:
            query = query.where(AlphaChatRun.turn_no < before_turn)
        runs = db.execute(query.order_by(AlphaChatRun.turn_no.desc()).limit(limit)).scalars().all()
        messages = []
        if runs:
            messages = db.execute(
                select(AlphaChatMessage)
                .join(AlphaChatRun, AlphaChatRun.id == AlphaChatMessage.run_id)
                .where(AlphaChatMessage.run_id.in_([run.id for run in runs]))
                .order_by(AlphaChatRun.turn_no, AlphaChatMessage.seq)
            ).scalars().all()
        latest = _latest_run(db, chat_id)
        return ChatHistory(
            messages=[ChatMessagePublic.model_validate(m) for m in messages],
            active_run_id=latest.id if latest and latest.status in ACTIVE_RUN_STATUSES else None,
            latest_run=ChatRunPublic.model_validate(latest) if latest else None,
            next_before_turn=runs[-1].turn_no if len(runs) == limit and runs[-1].turn_no > 1 else None,
        )


@router.get("/chats/{chat_id}/runs/{run_id}", response_model=ChatRunPublic)
def get_run(
    chat_id: uuid.UUID,
    run_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
) -> ChatRunPublic:
    with alpha_session() as db:
        _owned_chat(db, user_id, chat_id)
        run = db.get(AlphaChatRun, run_id)
        if run is None or run.chat_id != chat_id:
            raise HTTPException(404, "run_not_found")
        return ChatRunPublic.model_validate(run)


async def _stream_run(run_id: uuid.UUID, request: Request, after: str | None = None):
    snapshot = await asyncio.to_thread(store.load_snapshot, run_id)
    if snapshot["run"]["status"] not in ACTIVE_RUN_STATUSES:
        yield _sse("snapshot", snapshot)
        yield _sse("done", {"run_id": str(run_id), "status": snapshot["run"]["status"]})
        return
    redis = get_redis()
    try:
        if after and not await redis.xrange(stream_key(str(run_id)), min=after, max=after, count=1):
            after = None
        if after is None:
            cursor, events = await replay_events(redis, str(run_id))
            snapshot = await asyncio.to_thread(store.load_snapshot, run_id)
            if snapshot["run"]["status"] not in ACTIVE_RUN_STATUSES:
                yield _sse("snapshot", snapshot)
                yield _sse("done", {"run_id": str(run_id), "status": snapshot["run"]["status"]})
                return
            for _, kind, data in events:
                apply_event(snapshot, kind, data)
            yield _sse("snapshot", snapshot, cursor)
        else:
            cursor = after
        while not await request.is_disconnected():
            events = await read_events(redis, str(run_id), cursor)
            for event_id, kind, data in events:
                cursor = event_id
                yield _sse(kind, data, event_id)
                if kind == "done":
                    return
            if not events:
                snapshot = await asyncio.to_thread(store.load_snapshot, run_id)
                if snapshot["run"]["status"] not in ACTIVE_RUN_STATUSES:
                    yield _sse("snapshot", snapshot)
                    yield _sse("done", {"run_id": str(run_id), "status": snapshot["run"]["status"]})
                    return
                yield ": keep-alive\n\n"
    finally:
        await redis.aclose()


@router.get("/chats/{chat_id}/events")
async def stream_chat_events(
    chat_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    user_id: uuid.UUID = Depends(current_user_id),
    after: str | None = None,
) -> StreamingResponse:
    with alpha_session() as db:
        _owned_chat(db, user_id, chat_id)
        run = db.get(AlphaChatRun, run_id)
        if run is None or run.chat_id != chat_id:
            raise HTTPException(404, "run_not_found")
    cursor = request.headers.get("Last-Event-ID") or after
    if cursor and not re.fullmatch(r"\d+-\d+", cursor):
        raise HTTPException(422, "invalid_event_cursor")
    return StreamingResponse(
        _stream_run(run_id, request, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.delete("/chats/{chat_id}")
def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id)) -> dict[str, str]:
    with alpha_session() as db:
        chat = _owned_chat(db, user_id, chat_id, lock=True)
        run = _latest_run(db, chat_id)
        if run and run.status in ACTIVE_RUN_STATUSES:
            raise HTTPException(409, "chat_busy")
        chat.is_deleted = True
        chat.updated_at = store._now()
        db.commit()
    return {"message": "Chat deleted successfully"}
