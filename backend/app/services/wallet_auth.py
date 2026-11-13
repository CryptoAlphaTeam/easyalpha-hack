"""SIWE authentication; EVM identity is independent of the configured networks."""

import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from eth_utils import is_checksum_address, to_checksum_address
from fastapi import HTTPException
from siwe import SiweMessage, VerificationError

from app.core.config import get_settings
from app.core.security import WALLET_JWT_AUDIENCE, WALLET_JWT_ISSUER, create_access_token
from app.models.auth import (
    WalletAuthResponse, WalletBindResponse, WalletChallengeRequest, WalletChallengeResponse,
)
from app.services.auth import AuthService
from app.services.supabase_client import get_supabase_service_role_client
from app.services.wallet_auth_redis import digest, get_wallet_challenge_store


WALLET_COOKIE = "screener_wallet_challenge"
logger = logging.getLogger(__name__)


def normalize_evm_address(address: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise HTTPException(status_code=400, detail="Invalid EVM address")
    body = address[2:]
    if body != body.lower() and body != body.upper() and not is_checksum_address(address):
        raise HTTPException(status_code=400, detail="Invalid EVM address checksum")
    return address.lower()


class WalletAuthSupport:
    def __init__(self, *, settings=None, store=None, supabase=None):
        self.settings = settings if settings is not None else get_settings()
        self._store = store
        self._supabase = supabase

    @property
    def store(self):
        if self._store is None:
            self._store = get_wallet_challenge_store()
        return self._store

    @property
    def supabase(self):
        if self._supabase is None:
            self._supabase = get_supabase_service_role_client()
        return self._supabase

    def validate_origin(self, origin: str | None) -> str:
        if not self.settings.wallet_auth_enabled:
            raise HTTPException(status_code=404, detail="Wallet authentication is disabled")
        try:
            self.settings.validate_wallet_auth()
        except RuntimeError as exc:
            logger.warning("Wallet authentication configuration rejected: %s", exc)
            raise HTTPException(status_code=503, detail="Wallet authentication is not configured") from exc
        if not origin or origin not in self.settings.wallet_allowed_frontend_origins:
            raise HTTPException(status_code=403, detail="Wallet frontend origin is not allowed")
        return origin

    def _rate_limit(self, bucket: str, subjects: list[tuple[str, str, int]]) -> None:
        try:
            allowed = self.store.rate_limit(bucket, subjects)
        except Exception as exc:
            logger.exception("Wallet challenge rate-limit store unavailable")
            raise HTTPException(status_code=503, detail="Wallet challenge service unavailable") from exc
        if not allowed:
            raise HTTPException(status_code=429, detail="Too many wallet authentication requests", headers={"Retry-After": "300"})


class WalletAuthService(WalletAuthSupport):
    def create_challenge(
        self, payload: WalletChallengeRequest, *, origin: str, browser_session: str,
        ip_address: str, user_id: str | None = None,
    ) -> WalletChallengeResponse:
        self.validate_origin(origin)
        address = normalize_evm_address(payload.address)
        if payload.chain_id not in self.settings.wallet_allowed_chain_ids:
            raise HTTPException(status_code=400, detail={"code": "unsupported_wallet_chain", "allowed_chain_ids": self.settings.wallet_allowed_chain_ids})
        if payload.purpose == "bind" and not user_id:
            raise HTTPException(status_code=401, detail="Login is required to bind a wallet")
        self._rate_limit("challenge", [("ip", ip_address, 30), ("address", address, 15), ("browser", browser_session, 15)])
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expires = now + timedelta(seconds=self.settings.wallet_challenge_ttl_seconds)
        challenge_id = str(uuid4())
        parsed_origin = urlsplit(origin)
        resources = [f"urn:stock-screener:wallet:{payload.purpose}"]
        if payload.purpose == "bind":
            resources.append(f"urn:stock-screener:user:{user_id}")
        message = SiweMessage(
            scheme=parsed_origin.scheme, domain=parsed_origin.netloc,
            address=to_checksum_address(address), uri=f"{origin}/login", version="1",
            chain_id=int(payload.chain_id), nonce=secrets.token_hex(16),
            issued_at=now.isoformat().replace("+00:00", "Z"),
            expiration_time=expires.isoformat().replace("+00:00", "Z"),
            request_id=challenge_id, resources=resources,
            statement="Sign in to Stock Screener." if payload.purpose == "login" else "Bind this wallet to your signed-in Stock Screener account.",
        )
        record = {
            "message": message.prepare_message(), "namespace": payload.namespace,
            "address": address, "chain_id": payload.chain_id, "nonce": message.nonce,
            "origin": origin, "purpose": payload.purpose,
            "user_id": user_id if payload.purpose == "bind" else None,
            "browser_hash": digest(browser_session), "expires_at": expires.timestamp(),
        }
        try:
            self.store.save(challenge_id, json.dumps(record, separators=(",", ":")), self.settings.wallet_challenge_ttl_seconds)
        except Exception as exc:
            logger.exception("Wallet challenge persistence failed")
            raise HTTPException(status_code=503, detail="Wallet challenge service unavailable") from exc
        return WalletChallengeResponse(challenge_id=challenge_id, message=record["message"], expires_at=str(message.expiration_time))

    def verify_challenge(
        self, challenge_id: str, signature: str, *, origin: str, browser_session: str,
        ip_address: str, purpose: str, user_id: str | None = None,
    ) -> dict:
        self.validate_origin(origin)
        self._rate_limit("verify-ip", [("ip", ip_address, 60)])
        try:
            raw = self.store.read(challenge_id)
        except Exception as exc:
            logger.exception("Wallet challenge lookup failed")
            raise HTTPException(status_code=503, detail="Wallet challenge service unavailable") from exc
        if not raw:
            raise HTTPException(status_code=401, detail="Wallet challenge expired or already used")
        record = json.loads(raw)
        now = datetime.now(timezone.utc)
        if (record.get("namespace") != "eip155"
            or record["expires_at"] <= now.timestamp() or record["origin"] != origin
            or not browser_session or not secrets.compare_digest(record["browser_hash"], digest(browser_session))
            or record["purpose"] != purpose
            or (purpose == "bind" and (not user_id or record["user_id"] != user_id))):
            raise HTTPException(status_code=401, detail="Invalid wallet challenge context")
        self._rate_limit("verify", [("address", record["address"], 30), ("browser", browser_session, 30), ("challenge", challenge_id, 5)])
        if record["chain_id"] not in self.settings.wallet_allowed_chain_ids:
            raise HTTPException(status_code=401, detail="Wallet chain is no longer allowed")
        try:
            message = SiweMessage.from_message(record["message"])
            if (message.prepare_message() != record["message"]
                or message.address.lower() != record["address"]
                or str(message.chain_id) != record["chain_id"] or message.request_id != challenge_id
                or message.uri != f"{origin}/login"):
                raise ValueError("Wallet message context mismatch")
            # No provider: only EIP-191 address recovery, never ERC-1271 fallback.
            message.verify(signature, scheme=urlsplit(origin).scheme, domain=urlsplit(origin).netloc, nonce=record["nonce"], timestamp=now)
        except (VerificationError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid wallet signature") from exc
        try:
            consumed = self.store.consume(challenge_id, raw)
        except Exception as exc:
            logger.exception("Wallet challenge consumption failed")
            raise HTTPException(status_code=503, detail="Wallet challenge service unavailable") from exc
        if not consumed:
            raise HTTPException(status_code=401, detail="Wallet challenge expired or already used")
        return record

    def _resolve_identity(self, record: dict, ip_address: str) -> dict:
        try:
            response = self.supabase.rpc("screener_resolve_wallet_identity", {
                "p_namespace": record["namespace"], "p_identity_scope": "eoa", "p_address": record["address"],
                "p_purpose": record["purpose"], "p_user_id": record["user_id"],
                "p_auto_register": self.settings.wallet_auto_register_enabled, "p_ip_address": ip_address,
            }).execute()
        except Exception as exc:
            logger.exception("Wallet account RPC failed")
            raise HTTPException(status_code=503, detail="Wallet account service unavailable; request a new challenge to retry") from exc
        rows = response.data or []
        if not rows:
            raise HTTPException(status_code=503, detail="Wallet account service returned no result")
        result = rows[0]
        if result["status"] == "registration_disabled":
            raise HTTPException(status_code=403, detail={"code": "wallet_registration_disabled", "message": "Sign in with an existing account and bind this wallet"})
        if result["status"] == "conflict":
            raise HTTPException(status_code=409, detail="Wallet is already bound to another account")
        if result["status"] != "ok":
            raise HTTPException(status_code=401, detail="Wallet account does not exist")
        return result

    def login(self, record: dict, ip_address: str) -> WalletAuthResponse:
        result = self._resolve_identity(record, ip_address)
        auth = AuthService()
        auth.settings = self.settings
        auth._supabase = self.supabase
        user = auth._find_user_by_id(str(result["user_id"]))
        if not user:
            raise HTTPException(status_code=401, detail="Wallet account does not exist")
        token = create_access_token({
            "sub": str(user["id"]), "email": user.get("email") or "", "phone": user.get("phone"),
            "iss": WALLET_JWT_ISSUER, "aud": WALLET_JWT_AUDIENCE, "amr": ["wallet"],
            "wallet_chain_id": record["chain_id"],
        }, self.settings.wallet_auth_jwt_secret, self.settings.wallet_auth_token_ttl_seconds)
        return WalletAuthResponse(access_token=token, user=auth._build_auth_user(user), is_new_user=result["is_new_user"])

    def bind(self, record: dict, ip_address: str) -> WalletBindResponse:
        result = self._resolve_identity(record, ip_address)
        return WalletBindResponse(namespace=record["namespace"], address=record["address"], user_id=str(result["user_id"]))
