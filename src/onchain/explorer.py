"""Etherscan/Blockscout-compatible explorer API client (optional but recommended).

Enables the checks raw JSON-RPC cannot do: an address's first transactions
(funding source) and verified contract source.
"""
from __future__ import annotations

import time

import requests

from . import config

LAST_ERROR: str | None = None   # most recent request failure, for diagnostics

# Explorer hosts behind Cloudflare bot protection 403 the default
# python-requests User-Agent while serving browsers normally — identify as a
# browser for this public read-only API.
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}


def _v2_base() -> str | None:
    """Blockscout v2 REST base derived from the configured API URL — modern
    hosted instances often disable the legacy Etherscan-compat API and serve
    only v2 (https://host/api/v2/...)."""
    url = config.EXPLORER_API_URL
    if not url:
        return None
    base = url[:-4] if url.endswith("/api") else url
    return base + "/api/v2"


def _get_v2(path: str) -> tuple[int | None, dict | list | None]:
    """(status_code, body). 404 is a meaningful answer (e.g. unverified
    contract), any transport failure is (None, None) with LAST_ERROR set."""
    global LAST_ERROR
    base = _v2_base()
    if not base:
        return None, None
    try:
        resp = requests.get(base + path, timeout=8, headers=_HEADERS)
        if resp.status_code == 404:
            return 404, None
        resp.raise_for_status()
        LAST_ERROR = None
        return resp.status_code, resp.json()
    except (requests.RequestException, ValueError) as exc:
        LAST_ERROR = f"{type(exc).__name__}: {str(exc)[:160]}"
        return None, None


def health() -> dict:
    """Live probe with the exact failure. Reports which API generation works:
    'etherscan-compat' (everything available) or 'v2-rest' (holders + source
    available; funding tracing needs the legacy txlist API and stays dark)."""
    if not config.EXPLORER_API_URL:
        return {"configured": False, "ok": False, "level": None,
                "error": "EXPLORER_API_URL not set in .env"}
    if _get({"module": "block", "action": "eth_block_number"}) is not None:
        return {"configured": True, "ok": True, "level": "etherscan-compat",
                "error": None}
    v1_err = LAST_ERROR
    status, body = _get_v2("/stats")
    if status == 200 and isinstance(body, dict):
        return {"configured": True, "ok": True, "level": "v2-rest",
                "error": f"legacy API unavailable ({v1_err}); using v2 REST — "
                         "holders+source active, funding tracing unavailable"}
    return {"configured": True, "ok": False, "level": None,
            "error": LAST_ERROR or v1_err or "empty response from explorer"}


def _get(params: dict) -> dict | list | None:
    # plain requests.get (no shared Session): callers parallelize these
    # lookups across threads, and a Session is not guaranteed thread-safe.
    if not config.EXPLORER_API_URL:
        return None
    if config.EXPLORER_API_KEY:
        params = {**params, "apikey": config.EXPLORER_API_KEY}
    global LAST_ERROR
    for attempt in range(2):
        try:
            resp = requests.get(config.EXPLORER_API_URL, params=params,
                                timeout=8, headers=_HEADERS)
            resp.raise_for_status()
            body = resp.json()
            # Etherscan-compat: status "0" with "No transactions found" is a valid empty.
            result = body.get("result")
            if isinstance(result, str) and "rate limit" in result.lower():
                raise requests.RequestException(result)
            LAST_ERROR = None
            return result
        except (requests.RequestException, ValueError) as exc:
            LAST_ERROR = f"{type(exc).__name__}: {str(exc)[:160]}"
            if attempt >= 1:      # final attempt: fail fast, no pointless sleep
                return None
            time.sleep(1)
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
    """Top token holders — one HTTP call vs replaying the token's whole
    Transfer history. Tries the Etherscan-compat action, then Blockscout v2
    REST (mapped to the same row shape). None when neither is available."""
    result = _get({"module": "token", "action": "tokenholderlist",
                   "contractaddress": contract, "page": 1, "offset": offset})
    if isinstance(result, list) and result:
        return result
    status, body = _get_v2(f"/tokens/{contract}/holders")
    items = (body or {}).get("items") if isinstance(body, dict) else None
    if not items:
        return None
    rows = []
    for it in items:
        addr = ((it.get("address") or {}).get("hash")
                if isinstance(it.get("address"), dict) else it.get("address"))
        val = it.get("value")
        if addr and val is not None:
            rows.append({"TokenHolderAddress": str(addr),
                         "TokenHolderQuantity": str(val)})
    return rows or None


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
    """Concatenated verified source, "" if unverified, None if the explorer is
    unavailable. Tries Etherscan-compat, then Blockscout v2 REST (where a 404
    on /smart-contracts/{addr} is the 'not verified' answer)."""
    result = _get({"module": "contract", "action": "getsourcecode", "address": address})
    if isinstance(result, list) and result:
        entry = result[0] if isinstance(result[0], dict) else {}
        return entry.get("SourceCode", "") or ""
    status, body = _get_v2(f"/smart-contracts/{address}")
    if status == 404:
        return ""            # explorer answered: not a verified contract
    if status == 200 and isinstance(body, dict):
        parts = [body.get("source_code") or ""]
        for extra in body.get("additional_sources") or []:
            if isinstance(extra, dict):
                parts.append(extra.get("source_code") or "")
        return "\n".join(p for p in parts if p)
    return None
