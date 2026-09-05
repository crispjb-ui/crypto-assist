"""DexScreener free REST API client — market data and discovery feeds. No key needed."""
from __future__ import annotations

import requests

BASE = "https://api.dexscreener.com"
_session = requests.Session()


def _get(path: str):
    resp = _session.get(f"{BASE}{path}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def token_pairs(token_address: str) -> list[dict]:
    """All pairs for a token across chains: price, liquidity, volume, pairCreatedAt."""
    body = _get(f"/latest/dex/tokens/{token_address}")
    return body.get("pairs") or []


def pair(chain_id: str, pair_address: str) -> dict | None:
    body = _get(f"/latest/dex/pairs/{chain_id}/{pair_address}")
    pairs = body.get("pairs") or []
    return pairs[0] if pairs else None


def latest_token_profiles() -> list[dict]:
    """Discovery feed: tokens that recently created a DexScreener profile."""
    body = _get("/token-profiles/latest/v1")
    return body if isinstance(body, list) else []


def latest_boosted() -> list[dict]:
    """Tokens currently paying for DexScreener boosts (promotion signal)."""
    body = _get("/token-boosts/latest/v1")
    return body if isinstance(body, list) else []


def search_pairs(query: str) -> list[dict]:
    """Free-text pair search (symbol or name). Caller must filter by chain and
    exact symbol — search matches loosely."""
    body = _get(f"/latest/dex/search?q={query}")
    return body.get("pairs") or []


def best_pair_for_token(token_address: str, chain_id: str | None = None) -> dict | None:
    """Highest-liquidity pair for the token, optionally restricted to one chain."""
    pairs = token_pairs(token_address)
    if chain_id:
        pairs = [p for p in pairs if p.get("chainId") == chain_id]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
