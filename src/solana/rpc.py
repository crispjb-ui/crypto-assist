"""Solana JSON-RPC client — stdlib + requests, mirroring onchain/rpc.py posture.

No event logs on Solana: analysis is built from account state
(getAccountInfo, getTokenLargestAccounts) and transaction history
(getSignaturesForAddress + getTransaction with jsonParsed encoding).
Set SOLANA_RPC_URL in .env (dRPC serves Solana mainnet).
"""
from __future__ import annotations

import os
import time

import requests

# importing onchain.config runs the .env loader, so SOLANA_RPC_URL from the
# same profile file is visible here
from ..onchain import config as _config  # noqa: F401

DEFAULT_TIMEOUT = 30
BATCH_MAX = 20          # getTransaction responses are large — keep batches small
SIG_PAGE_LIMIT = 1000
MAX_SIG_PAGES = int(os.environ.get("SOLANA_MAX_SIG_PAGES", "10"))


class SolRpcError(RuntimeError):
    pass


def require_url() -> str:
    url = os.environ.get("SOLANA_RPC_URL", "")
    if not url:
        raise SystemExit("SOLANA_RPC_URL is not set — add your Solana mainnet "
                         "RPC endpoint (dRPC serves it) to .env")
    return url


class SolRpc:
    def __init__(self, url: str | None = None):
        self.url = url or require_url()
        self._session = requests.Session()
        self._id = 0
        self.last_errors: list[str] = []   # per-item batch / retry errors

    def clone(self) -> "SolRpc":
        """Fresh session for a worker thread (requests.Session is not
        thread-safe to share)."""
        return SolRpc(self.url)

    def call(self, method: str, params: list, timeout: float = DEFAULT_TIMEOUT):
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method,
                "params": params}
        resp = self._session.post(self.url, json=body, timeout=timeout)
        resp.raise_for_status()
        out = resp.json()
        if "error" in out:
            raise SolRpcError(f"{method}: {out['error']}")
        return out.get("result")

    def batch(self, calls: list[tuple[str, list]]) -> list:
        """Ordered results; a failed item yields None rather than aborting."""
        results: list = []
        for i in range(0, len(calls), BATCH_MAX):
            chunk = calls[i:i + BATCH_MAX]
            body = [{"jsonrpc": "2.0", "id": j, "method": m, "params": p}
                    for j, (m, p) in enumerate(chunk)]
            resp = self._session.post(self.url, json=body,
                                      timeout=DEFAULT_TIMEOUT * 2)
            resp.raise_for_status()
            out = resp.json()
            if isinstance(out, dict):          # provider rejected the batch
                raise SolRpcError(f"batch: {out.get('error')}")
            by_id = {item.get("id"): item for item in out}
            for j in range(len(chunk)):
                item = by_id.get(j) or {}
                results.append(item.get("result"))
                if item.get("error"):
                    self.last_errors.append(str(item["error"])[:200])
            time.sleep(0.05)
        return results

    def transaction(self, sig: str, retries: int = 3) -> dict | None:
        """Single getTransaction with backoff — the fallback when a batch
        item came back empty (rate limit / batch rejection), so a missing
        transaction is a fact about the chain, not about the burst."""
        cfg = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
               "commitment": "confirmed"}
        for attempt in range(retries):
            try:
                res = self.call("getTransaction", [sig, cfg], timeout=12)
                if res is None:
                    self.last_errors.append(f"getTransaction returned null for {sig[:12]}…")
                return res
            except Exception as exc:
                self.last_errors.append(str(exc)[:200])
                time.sleep(0.4 * (attempt + 1))
        return None

    def probe_transaction(self, sig: str) -> dict:
        """Diagnostic: the raw outcome of several getTransaction variants for
        one signature, to tell 'node has no record' from 'encoding/version
        rejected' from 'rate limited'."""
        out: dict = {}
        variants = {
            "jsonParsed+v0": {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            "json+v0": {"encoding": "json", "maxSupportedTransactionVersion": 0},
            "jsonParsed-no-version": {"encoding": "jsonParsed"},
            "jsonParsed+finalized": {"encoding": "jsonParsed",
                                     "maxSupportedTransactionVersion": 0,
                                     "commitment": "finalized"},
        }
        for name, cfg in variants.items():
            try:
                res = self.call("getTransaction", [sig, cfg], timeout=15)
                if res is None:
                    out[name] = "null (node returned no record)"
                else:
                    meta = res.get("meta") or {}
                    out[name] = (f"OK slot={res.get('slot')} version="
                                 f"{res.get('version')} err={meta.get('err')}")
            except Exception as exc:
                out[name] = f"ERROR {str(exc)[:160]}"
            time.sleep(0.3)
        return out

    def token_accounts_for_mint(self, owner: str, mint: str) -> list[str]:
        """Token account pubkeys `owner` holds for `mint` (mint filter — the
        query the balance check already relies on)."""
        out = self.call("getTokenAccountsByOwner",
                        [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        return [acc["pubkey"] for acc in (out or {}).get("value") or []]

    # --- history ---
    def signatures(self, address: str, before: str | None = None,
                   limit: int = SIG_PAGE_LIMIT) -> list[dict]:
        params: list = [address, {"limit": limit}]
        if before:
            params[1]["before"] = before
        return self.call("getSignaturesForAddress", params) or []

    def signatures_full(self, address: str,
                        max_pages: int = MAX_SIG_PAGES) -> tuple[list[dict], bool]:
        """(signatures oldest-first, history_complete). Incomplete history
        means the earliest window was NOT reached — launch analysis on it
        would be a lie, so callers must degrade explicitly."""
        pages: list[dict] = []
        before = None
        for _ in range(max_pages):
            page = self.signatures(address, before=before)
            pages.extend(page)
            if len(page) < SIG_PAGE_LIMIT:
                return list(reversed(pages)), True
            before = page[-1]["signature"]
        return list(reversed(pages)), False

    def transactions(self, sigs: list[str]) -> list[dict | None]:
        cfg = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
               "commitment": "confirmed"}
        return self.batch([("getTransaction", [s, cfg]) for s in sigs])

    # --- accounts / tokens ---
    def account_info(self, pubkey: str, parsed: bool = False) -> dict | None:
        enc = "jsonParsed" if parsed else "base64"
        out = self.call("getAccountInfo", [pubkey, {"encoding": enc}])
        return (out or {}).get("value")

    def multiple_accounts(self, pubkeys: list[str]) -> list[dict | None]:
        vals: list[dict | None] = []
        for i in range(0, len(pubkeys), 100):
            out = self.call("getMultipleAccounts",
                            [pubkeys[i:i + 100], {"encoding": "jsonParsed"}])
            vals.extend((out or {}).get("value") or [])
        return vals

    def token_supply(self, mint: str) -> dict:
        return (self.call("getTokenSupply", [mint]) or {}).get("value") or {}

    def token_largest(self, mint: str) -> list[dict]:
        return (self.call("getTokenLargestAccounts", [mint]) or {}).get("value") or []

    def token_accounts(self, owner: str) -> list[dict]:
        """All of `owner`'s token accounts (both token programs), jsonParsed:
        [{pubkey, mint, amount(raw), decimals, program}]."""
        out: list[dict] = []
        for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                     "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
            try:
                res = self.call("getTokenAccountsByOwner",
                                [owner, {"programId": prog},
                                 {"encoding": "jsonParsed"}])
            except SolRpcError:
                continue
            for acc in (res or {}).get("value") or []:
                try:
                    info = acc["account"]["data"]["parsed"]["info"]
                    ta = info["tokenAmount"]
                    out.append({"pubkey": acc["pubkey"], "mint": info["mint"],
                                "amount": int(ta["amount"]),
                                "decimals": int(ta.get("decimals") or 0),
                                "program": prog})
                except (KeyError, TypeError, ValueError):
                    continue          # unparsed account (odd extension set)
        return out

    def token_balance(self, owner: str, mint: str) -> int:
        """Current raw balance of `owner` in `mint`, summed over accounts."""
        out = self.call("getTokenAccountsByOwner",
                        [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0
        for acc in (out or {}).get("value") or []:
            info = acc["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total
