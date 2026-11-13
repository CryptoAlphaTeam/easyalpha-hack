"""Meteor/NEAR wallet authentication with NEP-413 and a registered login key."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException

from app.core.base58 import decode_base58
from app.core.security import (
    WALLET_JWT_AUDIENCE, WALLET_JWT_ISSUER, create_access_token, verify_access_token,
)
from app.models.auth import (
    NearWalletChallengeRequest, NearWalletChallengeResponse, NearWalletRotateRequest,
    NearWalletSignatureRequest, WalletAuthResponse, WalletBindResponse,
)
from app.services.auth import AuthService
from app.services.near_rpc import NearRPC, NearRPCError, validate_near_account_id
from app.services.wallet_auth import WalletAuthSupport
from app.services.wallet_auth_redis import digest


logger = logging.getLogger(__name__)


def nep413_digest(message: str, nonce: bytes, recipient: str) -> bytes:
    def borsh_string(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack("<I", len(encoded)) + encoded

    # The desktop integration omits the optional callback URL (Borsh None).
    payload = (
        struct.pack("<I", 2**31 + 413)
        + borsh_string(message) + nonce + borsh_string(recipient) + b"\x00"
    )
    return hashlib.sha256(payload).digest()


def _ed25519_key(public_key: str) -> Ed25519PublicKey:
    kind, _, encoded = public_key.partition(":")
    if kind != "ed25519" or not 32 <= len(encoded) <= 44:
        raise ValueError("Expected NEAR Ed25519 public key")
    return Ed25519PublicKey.from_public_bytes(decode_base58(encoded))


def _verify_signature(public_key: str, signature: str, message_hash: bytes) -> None:
    try:
        _ed25519_key(public_key).verify(base64.b64decode(signature, validate=True), message_hash)
    except (InvalidSignature, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid NEAR wallet signature") from exc


class NearWalletAuthService(WalletAuthSupport):
    def __init__(self, *, settings=None, store=None, supabase=None, rpc_factory=NearRPC):
        super().__init__(settings=settings, store=store, supabase=supabase)
        self.rpc_factory = rpc_factory

    def validate_origin(self, origin: str | None) -> str:
        if not getattr(self.settings, "wallet_near_auth_enabled", False):
            raise HTTPException(status_code=404, detail="NEAR wallet authentication is disabled")
        return super().validate_origin(origin)

    def _rpc(self, network: str) -> NearRPC:
        if network not in self.settings.wallet_near_allowed_networks:
            raise HTTPException(status_code=400, detail="Unsupported NEAR wallet network")
        try:
            return self.rpc_factory(self.settings, network)
        except NearRPCError as exc:
            raise HTTPException(status_code=503, detail="NEAR wallet RPC is not configured") from exc

    def create_challenge(
        self, payload: NearWalletChallengeRequest, *, origin: str, browser_session: str,
        ip_address: str, user_id: str | None = None,
    ) -> NearWalletChallengeResponse:
        self.validate_origin(origin)
        self._rpc(payload.chain_id)
        account_id = validate_near_account_id(payload.account_id)
        if payload.purpose != "login" and not user_id:
            raise HTTPException(status_code=401, detail="Login is required")
        if payload.new_public_key:
            try:
                _ed25519_key(payload.new_public_key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid NEAR replacement public key") from exc
        self._rate_limit("challenge", [
            ("ip", ip_address, 30), ("address", f"near:{payload.chain_id}:{account_id}", 15),
            ("browser", browser_session, 15),
        ])
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.settings.wallet_challenge_ttl_seconds)
        expires_at = expires.isoformat()
        challenge_id = uuid4()
        nonce = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        recipient = urlsplit(origin).netloc
        message = (
            "Stock Screener NEAR wallet authentication\n\n"
            f"Purpose: {payload.purpose}\nAccount: {account_id}\nNetwork: {payload.chain_id}\n"
            f"Origin: {origin}\nExpires At: {expires_at}\nRequest ID: {challenge_id}"
        )
        if payload.new_public_key:
            message += f"\nNew login key: {payload.new_public_key}"
        record = {
            "namespace": "near", "account_id": account_id, "network": payload.chain_id,
            "purpose": payload.purpose, "user_id": user_id, "origin": origin,
            "browser_hash": digest(browser_session), "expires_at": expires.timestamp(),
            "message": message, "recipient": recipient, "nonce": nonce,
            "new_public_key": payload.new_public_key,
        }
        try:
            self.store.save(str(challenge_id), json.dumps(record), self.settings.wallet_challenge_ttl_seconds)
        except Exception as exc:
            logger.exception("NEAR wallet challenge persistence failed")
            raise HTTPException(status_code=503, detail="Wallet challenge service unavailable") from exc
        return NearWalletChallengeResponse(
            challenge_id=challenge_id, message=message, recipient=recipient, nonce=nonce, expires_at=expires_at,
        )

    def verify_challenge(
        self, payload: NearWalletSignatureRequest, *, origin: str, browser_session: str,
        ip_address: str, purpose: str, user_id: str | None = None,
    ) -> dict:
        self.validate_origin(origin)
        challenge_id = str(payload.challenge_id)
        self._rate_limit("verify-ip", [("ip", ip_address, 60)])
        try:
            raw = self.store.read(challenge_id)
        except Exception as exc:
            logger.exception("NEAR wallet challenge lookup failed")
            raise HTTPException(status_code=503, detail="Wallet challenge service unavailable") from exc
        if not raw:
            raise HTTPException(status_code=401, detail="Wallet challenge expired or already used")
        record = json.loads(raw)
        if (
            record.get("namespace") != "near" or record["purpose"] != purpose
            or record["expires_at"] <= time.time() or record["origin"] != origin
            or not browser_session or not secrets.compare_digest(record["browser_hash"], digest(browser_session))
            or record["account_id"] != payload.account_id or record["user_id"] != user_id
            or (purpose != "login" and not user_id)
            or (purpose == "rotate" and record["new_public_key"] != payload.public_key)
        ):
            raise HTTPException(status_code=401, detail="Invalid NEAR wallet challenge context")
        self._rate_limit("verify", [
            ("address", f"near:{record['network']}:{payload.account_id}", 30),
            ("browser", browser_session, 30), ("challenge", challenge_id, 5),
        ])
        message_hash = nep413_digest(record["message"], base64.b64decode(record["nonce"]), record["recipient"])
        _verify_signature(payload.public_key, payload.signature, message_hash)
        previous_key = None
        if isinstance(payload, NearWalletRotateRequest) and payload.previous_public_key:
            previous_key = payload.previous_public_key
            if previous_key == payload.public_key:
                raise HTTPException(status_code=400, detail="The replacement key must differ from the previous key")
            _verify_signature(previous_key, payload.previous_signature, message_hash)
        rpc = self._rpc(record["network"])
        try:
            rpc.ensure_network()
            self._require_full_access(rpc, payload.account_id, payload.public_key)
            if previous_key:
                self._require_full_access(rpc, payload.account_id, previous_key)
        except NearRPCError as exc:
            logger.warning("NEAR access key lookup failed: %s (%s)", exc, exc.code)
            raise HTTPException(status_code=503, detail="NEAR wallet verification is temporarily unavailable") from exc
        try:
            consumed = self.store.consume(challenge_id, raw)
        except Exception as exc:
            logger.exception("NEAR wallet challenge consumption failed")
            raise HTTPException(status_code=503, detail="Wallet challenge service unavailable") from exc
        if not consumed:
            raise HTTPException(status_code=401, detail="Wallet challenge expired or already used")
        return {**record, "public_key": payload.public_key, "previous_public_key": previous_key}

    @staticmethod
    def _require_full_access(rpc: NearRPC, account_id: str, public_key: str) -> None:
        try:
            key = rpc.call("query", {
                "request_type": "view_access_key", "finality": "final",
                "account_id": account_id, "public_key": public_key,
            })
        except NearRPCError as exc:
            if exc.code in {"UNKNOWN_ACCOUNT", "UNKNOWN_ACCESS_KEY"}:
                raise HTTPException(status_code=401, detail="NEAR login key is absent") from exc
            raise
        if "permission" not in key:
            raise NearRPCError("NEAR access key permission is missing")
        if key["permission"] != "FullAccess":
            raise HTTPException(status_code=401, detail="NEAR signature requires a FullAccess key")

    def require_independent_login(self, token: str, user_id: str) -> None:
        try:
            claims = verify_access_token(token, self.settings.auth_jwt_secret)
        except (ValueError, TypeError):
            try:
                claims = verify_access_token(
                    token, self.settings.wallet_auth_jwt_secret,
                    issuer=WALLET_JWT_ISSUER, audience=WALLET_JWT_AUDIENCE,
                )
                # Legacy EVM tokens omit the namespace.
                if claims.get("amr") != ["wallet"] or claims.get("wallet_namespace") not in {None, "eip155", "solana"}:
                    claims = {}
            except (ValueError, TypeError):
                claims = {}
        issued_at = claims.get("iat")
        if str(claims.get("sub")) != user_id or type(issued_at) is not int or not 0 <= time.time() - issued_at <= 300:
            raise HTTPException(status_code=403, detail="A fresh independent login or previous key proof is required")

    def _resolve_identity(self, record: dict, ip_address: str) -> dict:
        try:
            response = self.supabase.rpc("screener_resolve_near_wallet_identity", {
                "p_network": record["network"], "p_account_id": record["account_id"],
                "p_public_key": record["public_key"], "p_purpose": record["purpose"],
                "p_user_id": record["user_id"], "p_previous_public_key": record["previous_public_key"],
                "p_auto_register": self.settings.wallet_auto_register_enabled, "p_ip_address": ip_address,
            }).execute()
        except Exception as exc:
            logger.exception("NEAR wallet identity RPC failed")
            raise HTTPException(status_code=503, detail="Wallet account service unavailable; request a new challenge") from exc
        if not response.data:
            raise HTTPException(status_code=503, detail="Wallet account service returned no result")
        result = response.data[0]
        if result["status"] == "registration_disabled":
            raise HTTPException(status_code=403, detail={"code": "wallet_registration_disabled"})
        if result["status"] == "conflict":
            raise HTTPException(status_code=409, detail="Wallet is already bound to another account")
        if result["status"] == "key_mismatch":
            raise HTTPException(status_code=403, detail={"code": "near_login_key_mismatch"})
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
            "wallet_namespace": "near", "wallet_chain_id": record["network"],
        }, self.settings.wallet_auth_jwt_secret, self.settings.wallet_auth_token_ttl_seconds)
        return WalletAuthResponse(
            access_token=token, user=auth._build_auth_user(user), is_new_user=result["is_new_user"],
        )

    def bind(self, record: dict, ip_address: str) -> WalletBindResponse:
        result = self._resolve_identity(record, ip_address)
        return WalletBindResponse(namespace="near", address=record["account_id"], user_id=str(result["user_id"]))
