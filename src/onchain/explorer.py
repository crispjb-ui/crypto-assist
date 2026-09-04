"""Etherscan/Blockscout-compatible explorer API client (optional but recommended).

Enables the checks raw JSON-RPC cannot do: an address's first transactions
(funding source) and verified contract source.
"""
from __future__ import annotations

import time

import requests

from . import config

_session = requests.Session()


def _get(params: dict) -> dict | list | None:
    if not config.EXPLORER_API_URL:
        return None
    if config.EXPLORER_API_KEY:
        params = {**params, "apikey": config.EXPLORER_API_KEY}
    for attempt in range(3):
        try:
            resp = _session.get(config.EXPLORER_API_URL, params=params, timeout=30)
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


def funding_source(address: str) -> str | None:
    """First address that sent native currency to this wallet, if traceable."""
    txs = first_transactions(address, limit=10)
    if not txs:
        return None
    addr = address.lower()
    for tx in txs:
        try:
            if tx.get("to", "").lower() == addr and int(tx.get("value", "0")) > 0:
                return tx.get("from", "").lower() or None
        except (ValueError, AttributeError):
            continue
    return None


def get_contract_source(address: str) -> str | None:
    """Concatenated verified source, "" if unverified, None if no explorer configured."""
    result = _get({"module": "contract", "action": "getsourcecode", "address": address})
    if not isinstance(result, list) or not result:
        return None
    entry = result[0] if isinstance(result[0], dict) else {}
    return entry.get("SourceCode", "") or ""
