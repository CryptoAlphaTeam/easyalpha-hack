"""JSON-RPC access and identifiers shared by NEAR login and billing."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from fastapi import HTTPException


NEAR_NETWORK_IDS = {"mainnet": 1, "testnet": 2}
NEAR_NETWORKS = {value: key for key, value in NEAR_NETWORK_IDS.items()}
NEAR_ACCOUNT_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
NEAR_SETTLEMENT_GRACE_SECONDS = 86400


def validate_near_account_id(account_id: str) -> str:
    if not 2 <= len(account_id) <= 64 or not NEAR_ACCOUNT_RE.fullmatch(account_id):
        raise HTTPException(status_code=400, detail="Invalid NEAR account ID")
    return account_id


class NearRPCError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", reason: str = ""):
        super().__init__(message)
        self.code = code
        self.reason = reason


class NearRPC:
    def __init__(self, settings, network: str):
        url = getattr(settings, "near_rpc_urls", {}).get(network, "")
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and parsed.hostname
        except ValueError:
            valid = False
        if network not in NEAR_NETWORK_IDS or not valid:
            raise NearRPCError("NEAR network RPC is not configured")
        self.url = url
        self.network = network
        self.timeout = settings.crypto_payment_rpc_timeout_seconds

    def call(self, method: str, params: dict | list) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": "screener", "method": method, "params": params})
        request = urllib.request.Request(
            self.url, data=body.encode(), headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (OSError, ValueError) as exc:
            raise NearRPCError(f"NEAR RPC request failed: {method}") from exc
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                cause = error.get("cause")
                cause = cause if isinstance(cause, dict) else {}
                info = cause.get("info")
                reason = info.get("cause", "") if isinstance(info, dict) else ""
                raise NearRPCError(
                    f"NEAR RPC returned an error: {method}",
                    code=cause.get("name") or error.get("name") or "",
                    reason=reason,
                )
            if isinstance(payload.get("result"), dict):
                return payload["result"]
        raise NearRPCError(f"Invalid NEAR RPC response: {method}")

    def ensure_network(self) -> None:
        if self.call("status", []).get("chain_id") != self.network:
            raise NearRPCError("NEAR RPC network does not match the requested network")
