"""Minimal EVM JSON-RPC client: retries, batching, and the few calls we need."""
from __future__ import annotations

import time
from typing import Any

import requests

from . import config


class RpcError(RuntimeError):
    pass


class EvmRpc:
    def __init__(self, url: str | None = None, timeout: int = 30):
        self.url = url or config.require_rpc()
        self.timeout = timeout
        self.session = requests.Session()
        self._id = 0

    def call(self, method: str, params: list[Any], retries: int = 4) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                resp = self.session.post(self.url, json=payload, timeout=self.timeout)
                if resp.status_code == 429:
                    raise RpcError("rate limited")
                if 400 <= resp.status_code < 500:
                    # Providers signal oversized/invalid queries via HTTP 4xx;
                    # surface as RpcError (non-retryable) so get_logs can
                    # shrink its range instead of retrying to death.
                    raise RpcError(f"http {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                body = resp.json()
                if "error" in body:
                    err = body["error"]
                    msg = err.get("message", str(err))
                    # Range/limit errors are handled by callers, not retried.
                    raise RpcError(msg)
                return body.get("result")
            except (requests.RequestException, RpcError) as exc:
                retryable = isinstance(exc, requests.RequestException) or "rate limited" in str(exc)
                if attempt >= retries or not retryable:
                    raise
                time.sleep(delay)
                delay *= 2

    def batch(self, calls: list[tuple[str, list[Any]]], chunk: int = 50) -> list[Any]:
        """Batched JSON-RPC. Falls back to sequential if the endpoint rejects batches."""
        out: list[Any] = []
        for i in range(0, len(calls), chunk):
            group = calls[i : i + chunk]
            payload = []
            for method, params in group:
                self._id += 1
                payload.append({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
            try:
                resp = self.session.post(self.url, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                body = resp.json()
                if not isinstance(body, list):
                    raise RpcError("batch not supported")
                by_id = {item["id"]: item for item in body}
                for req in payload:
                    item = by_id.get(req["id"], {})
                    if "error" in item:
                        out.append(None)
                    else:
                        out.append(item.get("result"))
            except (requests.RequestException, RpcError, KeyError, ValueError):
                for method, params in group:
                    try:
                        out.append(self.call(method, params))
                    except RpcError:
                        out.append(None)
        return out

    # --- convenience wrappers ---

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def latest_block(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_code(self, address: str, block: int | str = "latest") -> str:
        tag = hex(block) if isinstance(block, int) else block
        return self.call("eth_getCode", [address, tag]) or "0x"

    def get_nonce(self, address: str, block: int | str = "latest") -> int:
        tag = hex(block) if isinstance(block, int) else block
        return int(self.call("eth_getTransactionCount", [address, tag]), 16)

    def get_balance(self, address: str, block: int | str = "latest") -> int:
        tag = hex(block) if isinstance(block, int) else block
        return int(self.call("eth_getBalance", [address, tag]), 16)

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        return self.call("eth_call", [{"to": to, "data": data}, block]) or "0x"

    def get_storage_at(self, address: str, slot: str) -> str:
        return self.call("eth_getStorageAt", [address, slot, "latest"]) or "0x"

    def get_logs(self, from_block: int, to_block: int, address: str | None = None,
                 topics: list[Any] | None = None) -> list[dict]:
        """Chunked eth_getLogs; splits ranges the endpoint rejects."""
        logs: list[dict] = []
        step = config.MAX_LOG_BLOCK_RANGE
        start = from_block
        while start <= to_block:
            end = min(start + step - 1, to_block)
            flt: dict[str, Any] = {"fromBlock": hex(start), "toBlock": hex(end)}
            if address:
                flt["address"] = address
            if topics:
                flt["topics"] = topics
            try:
                logs.extend(self.call("eth_getLogs", [flt]))
                start = end + 1
            except RpcError:
                if step <= 100:
                    raise
                step //= 4  # endpoint's range limit is smaller than ours; shrink
        return logs

    def get_block_time(self, block: int) -> int:
        blk = self.call("eth_getBlockByNumber", [hex(block), False])
        return int(blk["timestamp"], 16) if blk else 0

    def estimate_block_rate(self, sample_blocks: int = 100_000) -> float:
        """Measured blocks per second. Robinhood Chain runs ~100-150ms blocks,
        so never assume a rate — derive time windows from a live sample."""
        hi = self.latest_block()
        lo = max(hi - sample_blocks, 0)
        hi_ts, lo_ts = self.get_block_time(hi), self.get_block_time(lo)
        if hi_ts <= lo_ts:
            return 4.0  # degenerate sample; conservative fallback
        return (hi - lo) / (hi_ts - lo_ts)

    def blocks_for_seconds(self, seconds: float,
                           floor: int = 50, cap: int = 20_000) -> int:
        return min(max(int(self.estimate_block_rate() * seconds), floor), cap)

    def find_deploy_block(self, address: str, lo: int = 0, hi: int | None = None) -> int:
        """First block where the address has code (binary search; needs historical state)."""
        hi = hi if hi is not None else self.latest_block()
        if self.get_code(address, hi) in ("0x", None):
            raise RpcError(f"{address} has no code at block {hi}")
        while lo < hi:
            mid = (lo + hi) // 2
            if self.get_code(address, mid) in ("0x", None):
                lo = mid + 1
            else:
                hi = mid
        return lo
