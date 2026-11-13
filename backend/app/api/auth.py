import re
import secrets
import urllib.parse

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.models.auth import (
    CurrentUserResponse, NearWalletChallengeRequest, NearWalletChallengeResponse,
    NearWalletRotateRequest, NearWalletSignatureRequest, WalletAuthResponse, WalletBindResponse, WalletChallengeRequest,
    WalletChallengeResponse, WalletSignatureRequest, SolanaWalletChallengeRequest,
    SolanaWalletSignatureRequest,
)
from app.services.auth import AuthService
from app.services.near_wallet_auth import NearWalletAuthService
from app.services.wallet_auth import WALLET_COOKIE, WalletAuthService
from app.services.solana_wallet_auth import SolanaWalletAuthService


router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/currentUser", response_model=CurrentUserResponse)
def current_user(authorization: str | None = Header(default=None)) -> CurrentUserResponse:
    token = _get_bearer_token(authorization)
    return AuthService().current_user_with_wallets(token)


@router.post("/wallet/challenge", response_model=WalletChallengeResponse)
def wallet_challenge(
    payload: WalletChallengeRequest, request: Request, response: Response,
    authorization: str | None = Header(default=None),
) -> WalletChallengeResponse:
    service = WalletAuthService()
    origin = service.validate_origin(request.headers.get("origin"))
    user_id = AuthService().current_user(_get_bearer_token(authorization)).id if payload.purpose == "bind" else None
    session = request.cookies.get(WALLET_COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{64}", session):
        session = secrets.token_hex(32)
    result = service.create_challenge(
        payload, origin=origin, browser_session=session,
        ip_address=_wallet_client_ip(request), user_id=user_id,
    )
    response.set_cookie(
        WALLET_COOKIE, session, max_age=service.settings.wallet_challenge_ttl_seconds,
        httponly=True, secure=origin.startswith("https://"), samesite="lax", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/wallet/verify", response_model=WalletAuthResponse)
def wallet_verify(payload: WalletSignatureRequest, request: Request, response: Response) -> WalletAuthResponse:
    service = WalletAuthService()
    origin = service.validate_origin(request.headers.get("origin"))
    ip_address = _wallet_client_ip(request)
    record = service.verify_challenge(
        str(payload.challenge_id), payload.signature, origin=origin,
        browser_session=request.cookies.get(WALLET_COOKIE, ""), ip_address=ip_address, purpose="login",
    )
    result = service.login(record, ip_address)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/wallet/bind", response_model=WalletBindResponse)
def wallet_bind(
    payload: WalletSignatureRequest, request: Request, response: Response,
    authorization: str | None = Header(default=None),
) -> WalletBindResponse:
    service = WalletAuthService()
    origin = service.validate_origin(request.headers.get("origin"))
    user_id = AuthService().current_user(_get_bearer_token(authorization)).id
    ip_address = _wallet_client_ip(request)
    record = service.verify_challenge(
        str(payload.challenge_id), payload.signature, origin=origin,
        browser_session=request.cookies.get(WALLET_COOKIE, ""), ip_address=ip_address,
        purpose="bind", user_id=user_id,
    )
    result = service.bind(record, ip_address)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/wallet/near/challenge", response_model=NearWalletChallengeResponse)
def near_wallet_challenge(
    payload: NearWalletChallengeRequest, request: Request, response: Response,
    authorization: str | None = Header(default=None),
) -> NearWalletChallengeResponse:
    service = NearWalletAuthService()
    origin = service.validate_origin(request.headers.get("origin"))
    user_id = AuthService().current_user(_get_bearer_token(authorization)).id if payload.purpose != "login" else None
    session = request.cookies.get(WALLET_COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{64}", session):
        session = secrets.token_hex(32)
    result = service.create_challenge(
        payload, origin=origin, browser_session=session, ip_address=_wallet_client_ip(request), user_id=user_id,
    )
    response.set_cookie(
        WALLET_COOKIE, session, max_age=service.settings.wallet_challenge_ttl_seconds,
        httponly=True, secure=origin.startswith("https://"), samesite="lax", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/wallet/near/verify", response_model=WalletAuthResponse)
def near_wallet_verify(payload: NearWalletSignatureRequest, request: Request, response: Response) -> WalletAuthResponse:
    service = NearWalletAuthService()
    ip_address = _wallet_client_ip(request)
    record = service.verify_challenge(
        payload, origin=request.headers.get("origin"), browser_session=request.cookies.get(WALLET_COOKIE, ""),
        ip_address=ip_address, purpose="login",
    )
    response.headers["Cache-Control"] = "no-store"
    return service.login(record, ip_address)


def _near_wallet_update(
    payload: NearWalletSignatureRequest, request: Request, response: Response,
    authorization: str | None, purpose: str,
) -> WalletBindResponse:
    service = NearWalletAuthService()
    origin = service.validate_origin(request.headers.get("origin"))
    token = _get_bearer_token(authorization)
    user_id = AuthService().current_user(token).id
    if isinstance(payload, NearWalletRotateRequest) and not payload.previous_public_key:
        service.require_independent_login(token, user_id)
    ip_address = _wallet_client_ip(request)
    record = service.verify_challenge(
        payload, origin=origin, browser_session=request.cookies.get(WALLET_COOKIE, ""),
        ip_address=ip_address, purpose=purpose, user_id=user_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return service.bind(record, ip_address)


@router.post("/wallet/near/bind", response_model=WalletBindResponse)
def near_wallet_bind(
    payload: NearWalletSignatureRequest, request: Request, response: Response,
    authorization: str | None = Header(default=None),
) -> WalletBindResponse:
    return _near_wallet_update(payload, request, response, authorization, "bind")


@router.post("/wallet/near/rotate", response_model=WalletBindResponse)
def near_wallet_rotate(
    payload: NearWalletRotateRequest, request: Request, response: Response,
    authorization: str | None = Header(default=None),
) -> WalletBindResponse:
    return _near_wallet_update(payload, request, response, authorization, "rotate")


def _wallet_client_ip(request: Request) -> str:
    # Uvicorn's trusted proxy configuration resolves request.client. Do not
    # accept arbitrary forwarded headers as the wallet rate-limit identity.
    return request.client.host if request.client else "unknown"



def _get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header is required")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Bearer token is required")

    return token.strip()


def _get_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # The first address is the original client when proxies append their IPs.
        client_ip = forwarded_for.split(",", maxsplit=1)[0].strip()
        if client_ip:
            return client_ip

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        client_ip = real_ip.strip()
        if client_ip:
            return client_ip

    return request.client.host if request.client else None


def _redirect_with_query(url: str, params: dict[str, str]) -> RedirectResponse:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(params.items())
    target = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    return RedirectResponse(target, status_code=302)
