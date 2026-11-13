from __future__ import annotations

import json
import base64
import binascii
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.core.base58 import decode_base58
from app.core.config import get_settings
from app.models.auth import AuthUser
from app.services.billing import _effective_pro, _other_provider_active_for_user
from app.services.near_rpc import (
    NEAR_NETWORK_IDS, NEAR_NETWORKS, NEAR_SETTLEMENT_GRACE_SECONDS,
    NearRPC, NearRPCError, validate_near_account_id,
)
from app.services.supabase_client import get_supabase_service_role_client
from app.services.wallet_auth import normalize_evm_address
from app.services.solana_wallet_auth import normalize_solana_address


logger = logging.getLogger(__name__)


PaymentInterval = Literal["monthly", "yearly"]
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEX_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOLANA_TX_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,100}$")
PAYMENT_ASSETS = {"monad": "MON", "solana": "SOL", "near": "NEAR"}
PAYMENT_CHAINS = {"eip155": "monad", "solana": "solana", "near": "near"}


class CryptoPaymentRPCError(RuntimeError):
    """A recoverable chain RPC failure."""


@dataclass(frozen=True)
class NativeTransferVerification:
    status: Literal["confirming", "paid"]
    tx_hash: str
    from_address: str
    to_address: str
    amount_atomic: int
    block_number: int | None
    confirmations: int
    receipt_status: str | None
    included_at: datetime | None = None


class EvmNativeTransferVerifier:
    """Verify direct or contract-executed native transfers using JSON-RPC."""

    def __init__(self, *, rpc_url: str, chain_id: int, confirmations: int, timeout: float = 10):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.confirmations = confirmations
        self.timeout = timeout
        self._request_id = 0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.rpc_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise CryptoPaymentRPCError(f"RPC request failed: {method}") from exc
        if payload.get("error"):
            raise CryptoPaymentRPCError(f"RPC returned an error for {method}")
        return payload.get("result")

    @staticmethod
    def _quantity(value: Any, field: str) -> int:
        if not isinstance(value, str) or not value.startswith("0x"):
            raise CryptoPaymentRPCError(f"RPC returned an invalid {field}")
        try:
            return int(value, 16)
        except ValueError as exc:
            raise CryptoPaymentRPCError(f"RPC returned an invalid {field}") from exc

    def verify(
        self,
        *,
        tx_hash: str,
        expected_from: str,
        expected_to: str,
        expected_amount_atomic: int,
    ) -> NativeTransferVerification:
        if not TX_HASH_RE.fullmatch(tx_hash):
            raise HTTPException(status_code=400, detail="Invalid transaction hash")

        chain_hex = self._rpc("eth_chainId", [])
        if self._quantity(chain_hex, "chain id") != self.chain_id:
            raise CryptoPaymentRPCError("RPC endpoint returned an unexpected chain")

        transaction = self._rpc("eth_getTransactionByHash", [tx_hash])
        if transaction is None:
            # A submitted transaction may not be visible at every RPC node
            # immediately. Treat it as pending so the client can retry.
            return NativeTransferVerification(
                status="confirming", tx_hash=tx_hash,
                from_address=expected_from, to_address=expected_to,
                amount_atomic=0, block_number=None, confirmations=0,
                receipt_status=None,
            )

        actual_from = str(transaction.get("from") or "").lower()
        actual_to = str(transaction.get("to") or "").lower()
        is_direct = str(transaction.get("input") or "0x").lower() in {"0x", "0x0"}
        transaction_chain_id = transaction.get("chainId")
        if transaction_chain_id is not None and self._quantity(transaction_chain_id, "transaction chain id") != self.chain_id:
            raise HTTPException(status_code=400, detail="Transaction is on the wrong chain")
        actual_amount = self._quantity(transaction.get("value"), "transaction value")
        if is_direct:
            if actual_from != expected_from.lower():
                raise HTTPException(status_code=400, detail="Transaction sender does not match the order")
            if actual_to != expected_to.lower():
                raise HTTPException(status_code=400, detail="Transaction recipient does not match the order")
            if actual_amount < expected_amount_atomic:
                raise HTTPException(status_code=400, detail="Transaction amount is below the order amount")

        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return NativeTransferVerification(
                status="confirming", tx_hash=tx_hash,
                from_address=actual_from if is_direct else expected_from,
                to_address=actual_to if is_direct else expected_to,
                amount_atomic=actual_amount if is_direct else 0, block_number=None,
                confirmations=0, receipt_status=None,
            )
        receipt_status = str(receipt.get("status") or "").lower()
        if receipt_status != "0x1":
            raise HTTPException(status_code=400, detail="Transaction failed on chain")
        if not is_direct:
            actual_amount = self._internal_transfer_amount(
                tx_hash=tx_hash, transaction=transaction,
                expected_from=expected_from, expected_to=expected_to,
                expected_amount_atomic=expected_amount_atomic,
            )
            actual_from, actual_to = expected_from.lower(), expected_to.lower()
        block_number = self._quantity(receipt.get("blockNumber"), "receipt block number")
        head = self._quantity(self._rpc("eth_blockNumber", []), "block number")
        confirmations = max(0, head - block_number + 1)
        status: Literal["confirming", "paid"] = (
            "paid" if confirmations >= self.confirmations else "confirming"
        )
        return NativeTransferVerification(
            status=status, tx_hash=tx_hash,
            from_address=actual_from, to_address=actual_to,
            amount_atomic=actual_amount, block_number=block_number,
            confirmations=confirmations, receipt_status=receipt_status,
        )

    def _internal_transfer_amount(
        self, *, tx_hash: str, transaction: dict[str, Any],
        expected_from: str, expected_to: str, expected_amount_atomic: int,
    ) -> int:
        trace = self._rpc("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
        if not isinstance(trace, dict) or not trace.get("type"):
            raise CryptoPaymentRPCError("RPC returned an invalid transaction trace")
        # Bind the call tree to the transaction returned by the same RPC.
        for field in ("from", "to", "input"):
            if str(trace.get(field) or "").lower() != str(transaction.get(field) or "").lower():
                raise CryptoPaymentRPCError("RPC trace does not match the transaction")
        if self._quantity(trace.get("value"), "trace value") != self._quantity(transaction.get("value"), "transaction value"):
            raise CryptoPaymentRPCError("RPC trace does not match the transaction")

        pending = [trace]
        while pending:
            call = pending.pop()
            if not isinstance(call, dict) or not call.get("type"):
                raise CryptoPaymentRPCError("RPC returned an invalid call frame")
            # A reverted ancestor rolls back all of its children's transfers.
            if call.get("error"):
                continue
            if (
                str(call["type"]).upper() == "CALL"
                and str(call.get("from") or "").lower() == expected_from.lower()
                and str(call.get("to") or "").lower() == expected_to.lower()
                and str(call.get("input") or "0x").lower() in {"0x", "0x0"}
            ):
                amount = self._quantity(call.get("value"), "internal transfer value")
                if amount >= expected_amount_atomic:
                    return amount
            children = call.get("calls", [])
            if not isinstance(children, list):
                raise CryptoPaymentRPCError("RPC returned invalid call frames")
            pending.extend(children)
        raise HTTPException(status_code=400, detail="No successful native transfer matches the order")


class SolanaNativeTransferVerifier:
    """Verify a native SOL System Program transfer through Solana JSON-RPC."""

    def __init__(self, *, rpc_url: str, confirmations: int, timeout: float = 10):
        self.rpc_url = rpc_url
        self.confirmations = confirmations
        self.timeout = timeout
        self._request_id = 0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}).encode("utf-8")
        request = urllib.request.Request(self.rpc_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise CryptoPaymentRPCError(f"RPC request failed: {method}") from exc
        if payload.get("error"):
            raise CryptoPaymentRPCError(f"RPC returned an error for {method}")
        return payload.get("result")

    def verify(self, *, tx_hash: str, expected_from: str, expected_to: str, expected_amount_atomic: int) -> NativeTransferVerification:
        if not SOLANA_TX_RE.fullmatch(tx_hash):
            raise HTTPException(status_code=400, detail="Invalid Solana transaction signature")
        # A missing result is normal immediately after Phantom submits a transaction.
        result = self._rpc("getTransaction", [tx_hash, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
        if result is None:
            return NativeTransferVerification("confirming", tx_hash, expected_from, expected_to, 0, None, 0, None)
        meta = result.get("meta") or {}
        if meta.get("err"):
            raise HTTPException(status_code=400, detail="Transaction failed on chain")
        instructions = ((result.get("transaction") or {}).get("message") or {}).get("instructions") or []
        matches: list[tuple[str, str, int]] = []
        for instruction in instructions:
            parsed = instruction.get("parsed") if isinstance(instruction, dict) else None
            if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
                continue
            info = parsed.get("info") or {}
            try:
                source, destination, lamports = str(info["source"]), str(info["destination"]), int(info["lamports"])
            except (KeyError, TypeError, ValueError):
                continue
            matches.append((source, destination, lamports))
        expected_from = normalize_solana_address(expected_from)
        expected_to = normalize_solana_address(expected_to)
        transfer = next((item for item in matches if item[0] == expected_from and item[1] == expected_to and item[2] >= expected_amount_atomic), None)
        if transfer is None:
            raise HTTPException(status_code=400, detail="No successful native transfer matches the order")
        slot = result.get("slot")
        if not isinstance(slot, int) or slot < 0:
            raise CryptoPaymentRPCError("RPC returned an invalid transaction slot")
        statuses = self._rpc("getSignatureStatuses", [[tx_hash], {"searchTransactionHistory": True}])
        status_row = ((statuses or {}).get("value") or [None])[0]
        confirmation_status = str((status_row or {}).get("confirmationStatus") or "")
        confirmations = self.confirmations if confirmation_status == "finalized" else 1 if confirmation_status == "confirmed" else 0
        if confirmations <= 0:
            return NativeTransferVerification("confirming", tx_hash, expected_from, expected_to, transfer[2], slot, 0, confirmation_status or None)
        status: Literal["confirming", "paid"] = "paid" if confirmations >= self.confirmations else "confirming"
        return NativeTransferVerification(status, tx_hash, expected_from, expected_to, transfer[2], slot, confirmations, confirmation_status or None)


def _validate_near_tx_hash(tx_hash: str) -> None:
    try:
        if 32 <= len(tx_hash) <= 44 and len(decode_base58(tx_hash)) == 32:
            return
    except ValueError:
        pass
    raise HTTPException(status_code=400, detail="Invalid NEAR transaction hash")


class NearNativeTransferVerifier:
    def __init__(self, rpc: NearRPC):
        self.rpc = rpc

    def verify(
        self, *, tx_hash: str, expected_from: str, expected_to: str, expected_amount_atomic: int,
    ) -> NativeTransferVerification:
        _validate_near_tx_hash(tx_hash)
        pending = NativeTransferVerification("confirming", tx_hash, expected_from, expected_to, 0, None, 0, None)
        try:
            self.rpc.ensure_network()
            try:
                result = self.rpc.call("tx", {
                    "tx_hash": tx_hash, "sender_account_id": expected_from, "wait_until": "FINAL",
                })
            except NearRPCError as exc:
                if exc.code == "UNKNOWN_TRANSACTION" or (
                    exc.code == "TIMEOUT_ERROR" and exc.reason in {"", "PENDING", "NOT_OBSERVED"}
                ):
                    return pending
                raise
            if result.get("final_execution_status") in {
                "NONE", "INCLUDED", "EXECUTED_OPTIMISTIC", "INCLUDED_FINAL", "EXECUTED",
            }:
                return pending
            if result.get("final_execution_status") != "FINAL":
                raise NearRPCError("NEAR transaction finality is missing")
            outcome_block = self._verify_transfer(result, tx_hash, expected_from, expected_to, expected_amount_atomic)
            header = self.rpc.call("block", {"block_id": outcome_block})["header"]
            if header["hash"] != outcome_block:
                raise NearRPCError("NEAR transaction inclusion block does not match")
            height = int(header["height"])
            nanoseconds = int(header["timestamp_nanosec"])
            if height < 0 or nanoseconds <= 0:
                raise ValueError("Invalid NEAR inclusion block")
            included_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=nanoseconds // 1000)
        except (NearRPCError, KeyError, ValueError, TypeError, OverflowError) as exc:
            raise CryptoPaymentRPCError("NEAR transaction verification failed") from exc
        return NativeTransferVerification(
            "paid", tx_hash, expected_from, expected_to, expected_amount_atomic, height, 0, "finalized", included_at,
        )

    @staticmethod
    def _verify_transfer(result: dict, tx_hash: str, sender: str, recipient: str, amount: int) -> str:
        tx = result["transaction"]
        transaction_outcome = result["transaction_outcome"]
        if tx["hash"] != tx_hash or transaction_outcome["id"] != tx_hash:
            raise NearRPCError("NEAR RPC returned a different transaction")
        if sender == recipient or tx["signer_id"] != sender or tx["receiver_id"] != recipient:
            raise HTTPException(status_code=400, detail="NEAR transaction accounts do not match the order")
        actions = tx["actions"]
        if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict) or set(actions[0]) != {"Transfer"}:
            raise HTTPException(status_code=400, detail="Expected a single native NEAR Transfer")
        deposit = actions[0]["Transfer"]["deposit"]
        if not isinstance(deposit, str) or not re.fullmatch(r"[0-9]{1,39}", deposit):
            raise NearRPCError("NEAR RPC returned an invalid transfer amount")
        if int(deposit) != amount:
            raise HTTPException(status_code=400, detail="NEAR transfer amount does not match the order")
        status = result["status"]
        if isinstance(status, dict) and "Failure" in status:
            raise HTTPException(status_code=400, detail="NEAR transfer execution failed")
        if not isinstance(status, dict) or "SuccessValue" not in status:
            raise NearRPCError("NEAR RPC did not return a successful execution status")
        receipt_id = transaction_outcome["outcome"]["status"]["SuccessReceiptId"]
        for receipt in result["receipts_outcome"]:
            if receipt["id"] != receipt_id:
                continue
            outcome = receipt["outcome"]
            if "Failure" in outcome["status"]:
                raise HTTPException(status_code=400, detail="NEAR transfer receipt failed")
            if outcome["executor_id"] == recipient and "SuccessValue" in outcome["status"]:
                return transaction_outcome["block_hash"]
        raise NearRPCError("NEAR transfer receipt is missing or invalid")


@dataclass(frozen=True)
class NearContractVerification:
    tx_hash: str
    signer_id: str
    contract_id: str
    method_name: str
    plan: str
    amount_atomic: int
    block_number: int
    included_at: datetime
    subscription: dict[str, Any]


class NearSubscriptionContractVerifier:
    def __init__(self, rpc: NearRPC):
        self.rpc = rpc

    def verify(self, *, tx_hash: str, expected_from: str, expected_contract: str,
               expected_plan: str, expected_amount_atomic: int) -> NearContractVerification:
        _validate_near_tx_hash(tx_hash)
        try:
            self.rpc.ensure_network()
            result = self.rpc.call("tx_status", {
                "tx_hash": tx_hash, "sender_account_id": expected_from, "wait_until": "FINAL",
            })
            if result.get("final_execution_status") != "FINAL":
                raise NearRPCError("NEAR contract transaction is not final")
            tx = result["transaction"]
            if tx.get("hash") != tx_hash or tx.get("signer_id") != expected_from or tx.get("receiver_id") != expected_contract:
                raise HTTPException(status_code=400, detail="NEAR subscription transaction accounts do not match the order")
            actions = tx.get("actions") or []
            if len(actions) != 1 or not isinstance(actions[0], dict) or "FunctionCall" not in actions[0]:
                raise HTTPException(status_code=400, detail="Expected one NEAR subscription FunctionCall")
            call = actions[0]["FunctionCall"]
            method = call.get("method_name")
            if method not in {"subscribe", "renew"}:
                raise HTTPException(status_code=400, detail="Unexpected NEAR subscription method")
            args_raw = call.get("args")
            if isinstance(args_raw, str):
                args = json.loads(base64.b64decode(args_raw).decode("utf-8"))
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                raise NearRPCError("NEAR FunctionCall args are invalid")
            plan = str(args.get("plan") or "")
            if plan not in {"Monthly", "Yearly", "monthly", "yearly"}:
                raise HTTPException(status_code=400, detail="Invalid subscription plan")
            plan = plan.lower()
            if plan != expected_plan.lower():
                raise HTTPException(status_code=400, detail="NEAR subscription plan does not match the order")
            deposit = call.get("deposit")
            if not isinstance(deposit, str) or not deposit.isdigit() or int(deposit) != expected_amount_atomic:
                raise HTTPException(status_code=400, detail="NEAR subscription amount does not match the order")
            status = result.get("status") or {}
            if "Failure" in status:
                raise HTTPException(status_code=400, detail="NEAR subscription execution failed")
            if "SuccessValue" not in status:
                raise NearRPCError("NEAR subscription execution result is missing")
            for receipt in result.get("receipts_outcome") or []:
                if "Failure" in (receipt.get("outcome") or {}).get("status", {}):
                    raise HTTPException(status_code=400, detail="NEAR subscription receipt failed")
            block_hash = result["transaction_outcome"]["block_hash"]
            block = self.rpc.call("block", {"block_id": block_hash})["header"]
            timestamp_ns = int(block["timestamp_nanosec"])
            included_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=timestamp_ns // 1000)
            subscription = self._view_subscription(expected_contract, expected_from)
            expected_status = str(subscription.get("status") or "").lower()
            if expected_status != "active" or str(subscription.get("plan") or "").lower() != plan:
                raise HTTPException(status_code=409, detail="NEAR contract subscription state is not active")
            return NearContractVerification(tx_hash, expected_from, expected_contract, method, plan,
                                            expected_amount_atomic, int(block["height"]), included_at, subscription)
        except HTTPException:
            raise
        except (NearRPCError, KeyError, ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise CryptoPaymentRPCError("NEAR subscription contract verification failed") from exc

    def _view_subscription(self, contract_id: str, account_id: str) -> dict[str, Any]:
        args = base64.b64encode(json.dumps({"account_id": account_id}, separators=(",", ":")).encode()).decode()
        result = self.rpc.call("query", {"request_type": "call_function", "finality": "final",
                                           "account_id": contract_id, "method_name": "get_subscription",
                                           "args_base64": args})
        raw = result.get("result")
        if not raw:
            raise NearRPCError("NEAR subscription view returned no state")
        return json.loads(bytes(raw).decode("utf-8"))


class CryptoPaymentService:
    """Creates and confirms provider-neutral payment orders for crypto assets."""

    def __init__(self, *, settings=None, supabase=None, verifier=None):
        self.settings = settings if settings is not None else get_settings()
        self.supabase = supabase if supabase is not None else get_supabase_service_role_client()
        self._verifier = verifier

    def _ensure_enabled(self, chain: str) -> None:
        crypto_enabled = getattr(self.settings, "crypto_payments_enabled", False)
        if chain == "monad":
            if not crypto_enabled:
                raise HTTPException(status_code=404, detail="Crypto payments are disabled")
            if getattr(self.settings, "crypto_payment_chain_namespace", "") != "eip155":
                raise HTTPException(status_code=503, detail="Unsupported crypto payment chain")
            if int(getattr(self.settings, "crypto_payment_chain_id", 0)) <= 0:
                raise HTTPException(status_code=503, detail="Crypto payment chain is not configured")
            if not getattr(self.settings, "crypto_payment_rpc_url", "").startswith(("http://", "https://")):
                raise HTTPException(status_code=503, detail="Crypto payment RPC is not configured")
            treasury = str(getattr(self.settings, "crypto_payment_treasury_address", ""))
            if not HEX_ADDRESS_RE.fullmatch(treasury):
                raise HTTPException(status_code=503, detail="Crypto payment treasury is not configured")
            if int(getattr(self.settings, "crypto_payment_confirmations", 0)) < 1:
                raise HTTPException(status_code=503, detail="Invalid crypto payment confirmation count")
        elif chain == "solana":
            if not getattr(self.settings, "crypto_solana_payments_enabled", False):
                raise HTTPException(status_code=404, detail="Solana payments are disabled")
            if int(getattr(self.settings, "crypto_solana_chain_id", 0)) <= 0:
                raise HTTPException(status_code=503, detail="Solana payment chain is not configured")
            if not getattr(self.settings, "crypto_solana_rpc_url", "").startswith(("http://", "https://")):
                raise HTTPException(status_code=503, detail="Solana payment RPC is not configured")
            try:
                normalize_solana_address(str(getattr(self.settings, "crypto_solana_treasury_address", "")))
            except HTTPException as exc:
                raise HTTPException(status_code=503, detail="Solana payment treasury is not configured") from exc
            if int(getattr(self.settings, "crypto_solana_confirmations", 0)) < 1:
                raise HTTPException(status_code=503, detail="Invalid Solana payment confirmation count")
        elif chain == "near":
            if not getattr(self.settings, "crypto_near_payments_enabled", False):
                raise HTTPException(status_code=404, detail="NEAR payments are disabled")
            rpc = self._near_rpc(self.settings.crypto_near_network)
            payment_mode = getattr(self.settings, "crypto_near_payment_mode", "native")
            try:
                target = (self.settings.crypto_near_subscription_contract_id
                          if payment_mode == "contract" else self.settings.crypto_near_treasury_address)
                target = validate_near_account_id(target)
            except HTTPException as exc:
                label = "NEAR subscription contract" if payment_mode == "contract" else "NEAR payment treasury"
                raise HTTPException(status_code=503, detail=f"{label} is not configured") from exc
            try:
                rpc.ensure_network()
                rpc.call("query", {"request_type": "view_account", "finality": "final", "account_id": target})
            except NearRPCError as exc:
                raise HTTPException(status_code=503, detail="NEAR payment treasury could not be verified") from exc
        else:
            raise HTTPException(status_code=503, detail="Unsupported crypto payment chain")

    def _treasury(self, chain: str = "monad") -> str:
        if chain == "monad":
            return self.settings.crypto_payment_treasury_address.lower()
        if chain == "solana":
            return self.settings.crypto_solana_treasury_address
        return self.settings.crypto_near_treasury_address

    def _near_rpc(self, network: str) -> NearRPC:
        try:
            return NearRPC(self.settings, network)
        except NearRPCError as exc:
            raise HTTPException(status_code=503, detail="NEAR payment RPC is not configured") from exc

    def _catalog(self, interval: PaymentInterval, asset: str = "MON") -> tuple[int, int, int]:
        if asset == "MON":
            raw = self.settings.crypto_mon_monthly_amount_atomic if interval == "monthly" else self.settings.crypto_mon_yearly_amount_atomic
            decimals = 18
        elif asset == "SOL":
            raw = self.settings.crypto_solana_monthly_amount_atomic if interval == "monthly" else self.settings.crypto_solana_yearly_amount_atomic
            decimals = 9
        elif asset == "NEAR":
            raw = self.settings.crypto_near_monthly_amount_atomic if interval == "monthly" else self.settings.crypto_near_yearly_amount_atomic
            decimals = 24
        else:
            raise HTTPException(status_code=400, detail="Unsupported crypto asset")
        try:
            amount = int(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"{asset} {interval} price is not configured") from exc
        if amount <= 0 or (asset == "NEAR" and amount >= 2**128):
            raise HTTPException(status_code=503, detail=f"{asset} {interval} price is not configured")
        return amount, decimals, 30 if interval == "monthly" else 365

    def _verifier_for_order(
        self, order: dict[str, Any],
    ) -> EvmNativeTransferVerifier | SolanaNativeTransferVerifier | NearNativeTransferVerifier | NearSubscriptionContractVerifier:
        if self._verifier is not None:
            return self._verifier
        timeout = float(getattr(self.settings, "crypto_payment_rpc_timeout_seconds", 10))
        match order["chain_namespace"]:
            case "eip155":
                return EvmNativeTransferVerifier(
                    rpc_url=self.settings.crypto_payment_rpc_url, chain_id=int(self.settings.crypto_payment_chain_id),
                    confirmations=int(self.settings.crypto_payment_confirmations), timeout=timeout,
                )
            case "solana":
                return SolanaNativeTransferVerifier(
                    rpc_url=self.settings.crypto_solana_rpc_url,
                    confirmations=int(self.settings.crypto_solana_confirmations), timeout=timeout,
                )
            case "near":
                rpc = self._near_rpc(NEAR_NETWORKS.get(order["chain_id"], ""))
                if order.get("method") == "near_contract":
                    return NearSubscriptionContractVerifier(rpc)
                return NearNativeTransferVerifier(rpc)
            case _:
                raise HTTPException(status_code=503, detail="Unsupported crypto payment chain")

    def _has_active_pro(self, user_id: str) -> bool:
        try:
            stripe_rows = (
                self.supabase.table("screener_subscriptions")
                .select("plan,status,current_period_end")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            ).data or []
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Subscription lookup failed") from exc
        if _effective_pro(stripe_rows[0] if stripe_rows else None):
            return True
        if _other_provider_active_for_user(
            user_id, supabase=self.supabase, settings=self.settings
        ):
            return True
        try:
            response = (
                self.supabase.table("screener_payment_entitlements")
                .select("id")
                .eq("user_id", user_id)
                .eq("plan", "pro")
                .eq("status", "active")
                .gt("current_period_end", datetime.now(timezone.utc).isoformat())
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Payment entitlement lookup failed") from exc
        return bool(response.data)

    def _assert_bound_wallet(self, user_id: str, address: str, namespace: str, identity_scope: str) -> None:
        try:
            response = (
                self.supabase.table("screener_wallet_identities")
                .select("address")
                .eq("user_id", user_id)
                .eq("namespace", namespace)
                .eq("identity_scope", identity_scope)
                .eq("address", address)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Wallet identity service unavailable") from exc
        if not response.data:
            raise HTTPException(status_code=403, detail="Payment wallet must be bound to this account")

    @staticmethod
    def _serialize_order(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        if result.get("amount_atomic") is not None:
            result["amount_atomic"] = str(result["amount_atomic"])
        return result

    def _order_by_idempotency(self, user_id: str, idempotency_key: str) -> dict[str, Any] | None:
        try:
            rows = (
                self.supabase.table("screener_payment_orders")
                .select("*")
                .eq("user_id", user_id)
                .eq("idempotency_key", idempotency_key)
                .limit(1)
                .execute()
            ).data or []
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Payment order lookup failed") from exc
        return self._serialize_order(rows[0]) if rows else None

    def _create_near_order(
        self, user: AuthUser, *, interval: PaymentInterval, payer_address: str,
        amount_atomic: int, idempotency_key: str | None,
    ) -> dict[str, Any]:
        network = self.settings.crypto_near_network
        chain_id = NEAR_NETWORK_IDS[network]
        recipient = self._treasury("near")
        validate_near_account_id(payer_address)
        if payer_address == recipient:
            raise HTTPException(status_code=400, detail="NEAR payer and recipient must differ")
        self._assert_bound_wallet(user.id, payer_address, "near", network)
        existing = self._order_by_idempotency(user.id, idempotency_key) if idempotency_key else None
        rpc_idempotency_key = idempotency_key
        if existing and existing.get("status") == "expired":
            rpc_idempotency_key = f"{idempotency_key[:80]}:retry:{uuid4().hex[:24]}"
        if not existing and self._has_active_pro(user.id):
            raise HTTPException(status_code=409, detail="An active Pro subscription already exists")
        try:
            rows = self.supabase.rpc("screener_create_near_payment_order", {
                "p_user_id": user.id, "p_chain_id": chain_id, "p_payer_address": payer_address,
                "p_recipient_address": recipient, "p_amount_atomic": str(amount_atomic), "p_interval": interval,
                "p_ttl_seconds": self.settings.crypto_payment_order_ttl_seconds, "p_idempotency_key": rpc_idempotency_key,
            }).execute().data
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "23505":
                raise HTTPException(status_code=409, detail="A conflicting payment order already exists") from exc
            if code == "42501":
                raise HTTPException(status_code=403, detail="Payment wallet must be bound to this account") from exc
            if code == "55000":
                raise HTTPException(status_code=409, detail="An active Pro subscription already exists") from exc
            logger.exception("NEAR payment order creation RPC failed")
            raise HTTPException(status_code=500, detail="NEAR payment order creation failed") from exc
        if not rows:
            raise HTTPException(status_code=500, detail="NEAR payment order creation returned no result")
        return self._serialize_order(rows[0])

    @staticmethod
    def _legacy_idempotent_order(row: dict[str, Any]) -> dict[str, Any]:
        if row["chain_namespace"] == "near":
            raise HTTPException(status_code=409, detail="Idempotency-Key belongs to a NEAR order")
        return row

    def _create_near_contract_order(
        self, user: AuthUser, *, interval: PaymentInterval, payer_address: str,
        amount_atomic: int, idempotency_key: str | None,
    ) -> dict[str, Any]:
        network = self.settings.crypto_near_network
        chain_id = NEAR_NETWORK_IDS[network]
        contract_id = validate_near_account_id(self.settings.crypto_near_subscription_contract_id)
        validate_near_account_id(payer_address)
        self._assert_bound_wallet(user.id, payer_address, "near", network)
        rpc_idempotency_key = idempotency_key
        if idempotency_key:
            existing = self._order_by_idempotency(user.id, idempotency_key)
            if existing and existing.get("status") == "expired":
                rpc_idempotency_key = f"{idempotency_key[:80]}:retry:{uuid4().hex[:24]}"
        try:
            rows = self.supabase.rpc("screener_create_near_contract_payment_order", {
                "p_user_id": user.id, "p_chain_id": chain_id, "p_payer_address": payer_address,
                "p_contract_id": contract_id, "p_amount_atomic": str(amount_atomic),
                "p_interval": interval, "p_ttl_seconds": self.settings.crypto_payment_order_ttl_seconds,
                "p_idempotency_key": rpc_idempotency_key,
            }).execute().data
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "23505":
                raise HTTPException(status_code=409, detail="A conflicting NEAR contract order already exists") from exc
            if code == "42501":
                raise HTTPException(status_code=403, detail="Payment wallet must be bound to this account") from exc
            if code == "55000":
                raise HTTPException(status_code=409, detail="An active Pro subscription already exists") from exc
            logger.exception("NEAR contract order creation RPC failed")
            raise HTTPException(status_code=500, detail="NEAR contract order creation failed") from exc
        if not rows:
            raise HTTPException(status_code=500, detail="NEAR contract order creation returned no result")
        order = self._serialize_order(rows[0])
        companion = (self.supabase.table("screener_near_contract_orders")
                     .select("contract_id,contract_method,network,plan")
                     .eq("payment_order_id", order["id"]).limit(1).execute().data or [])
        if companion:
            order.update({"payment_mode": "contract", **companion[0]})
        return order

    def create_order(
        self,
        user: AuthUser,
        *,
        interval: PaymentInterval,
        payer_address: str,
        chain: Literal["monad", "solana", "near"] = "monad",
        asset: Literal["MON", "SOL", "NEAR"] = "MON",
        payment_mode: Literal["native", "contract"] = "native",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if PAYMENT_ASSETS.get(chain) != asset:
            raise HTTPException(status_code=400, detail="Crypto chain and asset do not match")
        self._ensure_enabled(chain)
        amount_atomic, amount_decimals, period_days = self._catalog(interval, asset)
        if chain == "near":
            if payment_mode == "contract":
                self._ensure_enabled("near")
                if getattr(self.settings, "crypto_near_payment_mode", "native") != "contract":
                    raise HTTPException(status_code=404, detail="NEAR contract payments are disabled")
                return self._create_near_contract_order(
                    user, interval=interval, payer_address=payer_address, amount_atomic=amount_atomic,
                    idempotency_key=idempotency_key,
                )
            return self._create_near_order(
                user, interval=interval, payer_address=payer_address, amount_atomic=amount_atomic,
                idempotency_key=idempotency_key,
            )
        namespace, identity_scope = ("eip155", "eoa") if chain == "monad" else ("solana", "ed25519")
        payer_address = normalize_evm_address(payer_address) if chain == "monad" else normalize_solana_address(payer_address)
        self._assert_bound_wallet(user.id, payer_address, namespace, identity_scope)
        if self._has_active_pro(user.id):
            raise HTTPException(status_code=409, detail="An active Pro subscription already exists")

        if idempotency_key:
            existing = self._order_by_idempotency(user.id, idempotency_key)
            if existing:
                return self._legacy_idempotent_order(existing)

        now = datetime.now(timezone.utc)
        order = {
            "id": str(uuid4()),
            "user_id": user.id,
            "provider": "crypto",
            "method": "evm_native" if chain == "monad" else "solana_native",
            "chain_namespace": self.settings.crypto_payment_chain_namespace if chain == "monad" else "solana",
            "chain_id": int(self.settings.crypto_payment_chain_id) if chain == "monad" else int(self.settings.crypto_solana_chain_id),
            "asset_type": "native",
            "asset_symbol": asset,
            "amount_atomic": str(amount_atomic),
            "amount_decimals": amount_decimals,
            "recipient_address": self._treasury(chain),
            "expected_payer_address": payer_address,
            "plan": "pro",
            "interval": interval,
            "period_days": period_days,
            "status": "created",
            "idempotency_key": idempotency_key,
            "expires_at": (now + timedelta(seconds=int(self.settings.crypto_payment_order_ttl_seconds))).isoformat(),
        }
        try:
            self.supabase.table("screener_payment_orders").insert(order).execute()
        except Exception as exc:
            if idempotency_key and getattr(exc, "code", None) in {"23505", "409"}:
                existing = self._order_by_idempotency(user.id, idempotency_key)
                if existing:
                    return self._legacy_idempotent_order(existing)
            raise HTTPException(status_code=500, detail="Payment order creation failed") from exc
        return self._serialize_order(order)

    def _load_order(self, user: AuthUser, order_id: UUID) -> dict[str, Any]:
        try:
            rows = (
                self.supabase.table("screener_payment_orders")
                .select("*")
                .eq("id", str(order_id))
                .eq("user_id", user.id)
                .limit(1)
                .execute()
            ).data or []
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Payment order lookup failed") from exc
        if not rows:
            raise HTTPException(status_code=404, detail="Payment order not found")
        return rows[0]

    def get_order(self, user: AuthUser, order_id: UUID) -> dict[str, Any]:
        row = self._load_order(user, order_id)
        is_near = row["chain_namespace"] == "near"
        if not is_near and not getattr(self.settings, "crypto_payments_enabled", False):
            if row["chain_namespace"] != "solana" or not getattr(self.settings, "crypto_solana_payments_enabled", False):
                raise HTTPException(status_code=404, detail="Crypto payments are disabled")
        if row.get("status") in {"created", "confirming"} and row.get("expires_at"):
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if is_near:
                expires += timedelta(seconds=NEAR_SETTLEMENT_GRACE_SECONDS)
            if expires <= datetime.now(timezone.utc):
                return self._update_open_order(user, order_id, {"status": "expired"})
        result = self._serialize_order(row)
        if row.get("method") == "near_contract":
            try:
                companion = (self.supabase.table("screener_near_contract_orders")
                             .select("contract_id,contract_method,network,plan,payment_id,subscription_status,subscription_expires_at")
                             .eq("payment_order_id", str(order_id)).limit(1).execute().data or [])
            except Exception as exc:
                raise HTTPException(status_code=503, detail="NEAR contract order lookup failed") from exc
            if companion:
                result.update({"payment_mode": "contract", **companion[0]})
        return result

    def _update_open_order(self, user: AuthUser, order_id: UUID, updates: dict[str, Any]) -> dict[str, Any]:
        try:
            rows = (
                self.supabase.table("screener_payment_orders").update(updates)
                .eq("id", str(order_id)).eq("user_id", user.id)
                .in_("status", ["created", "confirming"]).execute()
            ).data
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Payment order update failed") from exc
        return self._serialize_order(rows[0] if rows else self._load_order(user, order_id))

    def verify_order(self, user: AuthUser, order_id: UUID, tx_hash: str) -> dict[str, Any]:
        order = self.get_order(user, order_id)
        chain = PAYMENT_CHAINS.get(order["chain_namespace"])
        if chain is None:
            raise HTTPException(status_code=503, detail="Unsupported crypto payment chain")
        # The NEAR feature flag gates new orders. Existing orders can still settle.
        if chain != "near" and hasattr(self.settings, "crypto_payments_enabled"):
            self._ensure_enabled(chain)
        if chain == "monad":
            tx_hash = tx_hash.lower()
            if not TX_HASH_RE.fullmatch(tx_hash):
                raise HTTPException(status_code=400, detail="Invalid transaction hash")
        elif chain == "solana":
            if not SOLANA_TX_RE.fullmatch(tx_hash):
                raise HTTPException(status_code=400, detail="Invalid Solana transaction signature")
        else:
            _validate_near_tx_hash(tx_hash)
        if order.get("status") == "paid":
            if chain == "near" and order["last_tx_hash"] != tx_hash:
                raise HTTPException(status_code=409, detail="Order was paid with a different transaction")
            return order
        if order.get("status") == "expired":
            raise HTTPException(status_code=409, detail="Payment order has expired")
        try:
            if order.get("method") == "near_contract":
                result = self._verifier_for_order(order).verify(
                    tx_hash=tx_hash, expected_from=order["expected_payer_address"],
                    expected_contract=order["recipient_address"], expected_plan=order["interval"],
                    expected_amount_atomic=int(order["amount_atomic"]),
                )
            else:
                result = self._verifier_for_order(order).verify(
                    tx_hash=tx_hash, expected_from=order["expected_payer_address"],
                    expected_to=order["recipient_address"], expected_amount_atomic=int(order["amount_atomic"]),
                )
        except CryptoPaymentRPCError as exc:
            raise HTTPException(status_code=503, detail="Blockchain payment verification is temporarily unavailable") from exc
        if isinstance(result, NearContractVerification):
            try:
                response = self.supabase.rpc("screener_confirm_near_contract_payment_order", {
                    "p_order_id": str(order_id), "p_user_id": user.id, "p_chain_id": order["chain_id"],
                    "p_tx_hash": result.tx_hash, "p_from_address": result.signer_id,
                    "p_contract_id": result.contract_id, "p_method_name": result.method_name,
                    "p_plan": result.plan, "p_amount_atomic": str(result.amount_atomic),
                    "p_block_number": result.block_number, "p_included_at": result.included_at.isoformat(),
                    "p_subscription": {**result.subscription, "status": result.subscription["status"].lower()},
                }).execute()
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code == "23505":
                    raise HTTPException(status_code=409, detail="Transaction has already been used") from exc
                if code == "22023":
                    raise HTTPException(status_code=409, detail="Contract subscription does not match the order") from exc
                if code == "55000":
                    raise HTTPException(status_code=409, detail="An active Pro subscription already exists") from exc
                logger.exception("NEAR contract confirmation RPC failed")
                raise HTTPException(status_code=500, detail="NEAR contract confirmation failed") from exc
            rows = response.data or []
            if not rows:
                raise HTTPException(status_code=500, detail="NEAR contract confirmation returned no result")
            return self._serialize_order(rows[0])
        if result.status == "confirming":
            current = self._update_open_order(user, order_id, {
                "status": "confirming", "last_tx_hash": result.tx_hash, "last_confirmations": result.confirmations,
            })
            if chain == "near" and current["status"] == "paid" and current["last_tx_hash"] != tx_hash:
                raise HTTPException(status_code=409, detail="Order was paid with a different transaction")
            if current["status"] == "expired":
                raise HTTPException(status_code=409, detail="Payment order has expired")
            return current

        params = {
            "p_order_id": str(order_id), "p_user_id": user.id, "p_chain_id": order["chain_id"],
            "p_tx_hash": result.tx_hash, "p_from_address": result.from_address, "p_to_address": result.to_address,
            "p_amount_atomic": str(result.amount_atomic), "p_block_number": result.block_number,
        }
        rpc = "screener_confirm_payment_order"
        if chain == "near":
            if result.included_at is None or result.receipt_status != "finalized":
                raise HTTPException(status_code=503, detail="NEAR final transfer result is incomplete")
            rpc = "screener_confirm_near_payment_order"
            params["p_included_at"] = result.included_at.isoformat()
        else:
            params.update({
                "p_chain_namespace": order["chain_namespace"], "p_confirmations": result.confirmations,
                "p_receipt_status": result.receipt_status,
            })
        try:
            response = self.supabase.rpc(rpc, params).execute()
        except Exception as exc:
            code = getattr(exc, "code", None)
            logger.exception(
                "Payment confirmation RPC failed",
                extra={
                    "payment_order_id": str(order_id),
                    "supabase_error_code": code,
                    "supabase_error_message": getattr(exc, "message", None) or str(exc),
                    "supabase_error_details": getattr(exc, "details", None),
                    "supabase_error_hint": getattr(exc, "hint", None),
                },
            )
            if code == "23505":
                raise HTTPException(status_code=409, detail="Transaction has already been used") from exc
            if code == "P0002":
                raise HTTPException(status_code=404, detail="Payment order not found") from exc
            if code == "22023":
                raise HTTPException(status_code=409, detail="Payment order expired or transaction does not match the order") from exc
            if code == "55000" and chain == "near":
                raise HTTPException(status_code=409, detail="An active Pro subscription already exists; payment needs reconciliation") from exc
            raise HTTPException(status_code=500, detail="Payment confirmation failed") from exc
        rows = response.data or []
        if not rows:
            raise HTTPException(status_code=500, detail="Payment confirmation returned no result")
        return self._serialize_order(rows[0])
