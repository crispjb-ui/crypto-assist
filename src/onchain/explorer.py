"""Etherscan/Blockscout-compatible explorer API client (optional but recommended).

Enables the checks raw JSON-RPC cannot do: an address's first transactions
(funding source) and verified contract source.
"""
from __future__ import annotations

import time

import requests

from . import config


def _get(params: dict) -> dict | list | None:
    # plain requests.get (no shared Session): callers parallelize these
    # lookups across threads, and a Session is not guaranteed thread-safe.
    if not config.EXPLORER_API_URL:
        return None
    if config.EXPLORER_API_KEY:
        params = {**params, "apikey": config.EXPLORER_API_KEY}
    for attempt in range(3):
        try:
            resp = requests.get(config.EXPLORER_API_URL, params=params, timeout=20)
            resp.raise_for_status()
            body = resp.json()
            # Etherscan-compat: status "0" with "No transactions found" is a valid empty.
            result = body.get("result")
            if isinstance(result, str) and "rate limit" in result.lower():
                raise requests.RequestException(result)
            return result
        except (requests.RequestException, ValueError):
            if attempt == 2:
                return None
            time.sleep(2 ** attempt)
    return None


def first_transactions(address: str, limit: int = 10) -> list[dict] | None:
    """Oldest transactions for an address, ascending. None = explorer unavailable."""
    result = _get({
        "module": "account", "action": "txlist", "address": address,
        "startblock": 0, "endblock": 99999999, "page": 1,
        "offset": limit, "sort": "asc",
    })
    return result if isinstance(result, list) else ([] if result is not None else None)


def internal_transactions(address: str, limit: int = 20) -> list[dict] | None:
    """Oldest internal (contract-mediated) transfers for an address, ascending.
    None = explorer unavailable. Captures funding sent through a disperser or
    airdrop contract, which external txlist alone misses."""
    result = _get({
        "module": "account", "action": "txlistinternal", "address": address,
        "startblock": 0, "endblock": 99999999, "page": 1,
        "offset": limit, "sort": "asc",
    })
    return result if isinstance(result, list) else ([] if result is not None else None)


def funding_event(address: str) -> dict | None:
    """Earliest inbound native transfer to this wallet across external AND
    internal transfers. Returns {from, hash, internal, block} or None.

    The `from` of an internal transfer is the disperser/airdrop CONTRACT, not
    the human funder — callers resolve the true origin from the parent tx.
    Cost control: the internal-transfer lookup (second HTTP call) runs only
    when the wallet's FIRST external transaction isn't already an inbound
    value transfer — a wallet whose very first tx is direct funding cannot
    have been disperser-funded earlier than that.
    """
    addr = address.lower()
    ext = first_transactions(address, limit=10)
    candidates: list[dict] = []
    first_ext_is_funding = False
    for i, tx in enumerate(ext or []):
        try:
            if tx.get("to", "").lower() == addr and int(tx.get("value", "0")) > 0:
                candidates.append({"from": tx.get("from", "").lower(),
                                   "hash": tx.get("hash"), "internal": False,
                                   "block": int(tx.get("blockNumber", "0"))})
                if i == 0:
                    first_ext_is_funding = True
        except (ValueError, AttributeError):
            continue
    if not first_ext_is_funding:
        for tx in internal_transactions(address, limit=10) or []:
            try:
                if tx.get("to", "").lower() == addr and int(tx.get("value", "0")) > 0:
                    candidates.append({"from": tx.get("from", "").lower(),
                                       "hash": tx.get("hash"), "internal": True,
                                       "block": int(tx.get("blockNumber", "0"))})
            except (ValueError, AttributeError):
                continue
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["block"])


def token_holder_list(contract: str, offset: int = 500) -> list[dict] | None:
    """Top token holders via Blockscout's Etherscan-compatible endpoint —
    one HTTP call vs replaying the token's whole Transfer history. None when
    the explorer is unavailable or the action unsupported."""
    result = _get({"module": "token", "action": "tokenholderlist",
                   "contractaddress": contract, "page": 1, "offset": offset})
    return result if isinstance(result, list) and result else None


_creation_cache: dict[str, str | None] = {}


def contract_creation_tx(address: str) -> str | None:
    """Creation tx hash via the explorer (1 call) — much cheaper than a
    ~25-call deploy-block binary search. None when unavailable. Memoized:
    launch analysis and the curve check both need it for the same pair."""
    key = address.lower()
    if key in _creation_cache:
        return _creation_cache[key]
    result = _get({"module": "contract", "action": "getcontractcreation",
                   "contractaddresses": address})
    txh = None
    if isinstance(result, list) and result and isinstance(result[0], dict):
        txh = result[0].get("txHash") or result[0].get("txhash")
    if txh is not None or result is not None:  # don't cache outages
        _creation_cache[key] = txh
    return txh


def funding_source(address: str) -> str | None:
    """First address that sent native currency to this wallet, if traceable."""
    ev = funding_event(address)
    return ev["from"] if ev else None


def get_contract_source(address: str) -> str | None:
    """Concatenated verified source, "" if unverified, None if no explorer configured."""
    result = _get({"module": "contract", "action": "getsourcecode", "address": address})
    if not isinstance(result, list) or not result:
        return None
    entry = result[0] if isinstance(result[0], dict) else {}
    return entry.get("SourceCode", "") or ""
