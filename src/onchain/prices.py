"""USD pricing for the quote currencies wallet PnL is measured in.

Realized PnL is recorded in quote units (ETH for Pons curves, the paired
stock token for Long pools) and those units are never summed across quotes —
that stays true. This module only converts each quote to USD *at the current
DexScreener price* so the leaderboard can show one comparable number. The
conversion is display-only and approximate by construction: PnL was realized
at past prices, the USD figure marks what those units are worth now.

Symbol -> address comes from the quote_tokens ledger (recorded at ingest
time, exact). Symbols ingested before that table existed are resolved once
via DexScreener search restricted to this chain and an exact symbol match on
the base token; a symbol that cannot be resolved is reported unpriced, never
guessed.
"""
from __future__ import annotations

import time

from . import config, dexscreener, store

_CACHE_SECONDS = 300
# chain -> (fetched_at, prices, symbols already attempted this window)
_cache: dict[str, tuple[float, dict[str, float], set[str]]] = {}

NATIVE_SYMBOLS = {"ETH", "WETH", "BNB", "WBNB"}


def _price_from_pair(pair: dict) -> float | None:
    try:
        p = float(pair.get("priceUsd") or 0)
        return p if p > 0 else None
    except (TypeError, ValueError):
        return None


def _native_usd_from_pair(pair: dict) -> float | None:
    """priceUsd / priceNative of any pair = USD per native token."""
    try:
        usd = float(pair.get("priceUsd") or 0)
        nat = float(pair.get("priceNative") or 0)
        return usd / nat if usd > 0 and nat > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _resolve_symbol(symbol: str, chain: str) -> str | None:
    """One-time search fallback for quote symbols recorded before addresses
    were kept. Exact base-symbol match on this chain, highest liquidity."""
    try:
        pairs = dexscreener.search_pairs(symbol)
    except Exception:
        return None
    candidates = [
        p for p in pairs
        if p.get("chainId") == chain
        and ((p.get("baseToken") or {}).get("symbol") or "").upper()
        == symbol.upper()
    ]
    if not candidates:
        return None
    best = max(candidates,
               key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    addr = (best.get("baseToken") or {}).get("address")
    if addr:
        store.remember_quote_token(symbol, addr)  # resolve once, keep forever
    return addr


def quote_prices_usd(symbols: list[str]) -> dict[str, float]:
    """Current USD price per quote symbol. Missing keys = unpriced (no
    address known and search found no exact match on this chain)."""
    chain = config.DEXSCREENER_CHAIN_ID
    if not chain:
        return {}
    now = time.time()
    cached = _cache.get(chain)
    if cached and now - cached[0] < _CACHE_SECONDS \
            and set(symbols) <= cached[2]:
        return cached[1]

    known = store.quote_token_map()
    prices: dict[str, float] = {}
    native_usd: float | None = None
    for sym in dict.fromkeys(symbols):
        if sym in NATIVE_SYMBOLS:
            continue                      # priced from any pair's ratio below
        addr = known.get(sym) or _resolve_symbol(sym, chain)
        if not addr:
            continue
        try:
            pair = dexscreener.best_pair_for_token(addr, chain)
        except Exception:
            continue
        if not pair:
            continue
        p = _price_from_pair(pair)
        if p is not None:
            prices[sym] = p
        if native_usd is None:
            native_usd = _native_usd_from_pair(pair)

    wanted_native = [s for s in symbols if s in NATIVE_SYMBOLS]
    if wanted_native:
        if native_usd is None:
            # no priced quote pair to ratio from — find any WETH-symbol pair
            try:
                for p in dexscreener.search_pairs("WETH"):
                    if p.get("chainId") == chain:
                        native_usd = _native_usd_from_pair(p)
                        if native_usd:
                            break
            except Exception:
                pass
        if native_usd:
            for s in wanted_native:
                prices[s] = native_usd

    _cache[chain] = (now, prices, set(symbols))
    return prices


def usd_realized(realized_by_quote: dict[str, float],
                 prices: dict[str, float]) -> tuple[float | None, list[str]]:
    """(usd_total, unpriced_symbols). usd_total is None when NOTHING could be
    priced; unpriced quotes are named so a partial total is never mistaken
    for a complete one."""
    total, priced_any, unpriced = 0.0, False, []
    for sym, units in realized_by_quote.items():
        p = prices.get(sym)
        if p is None:
            unpriced.append(sym)
        else:
            total += units * p
            priced_any = True
    return (total if priced_any else None), unpriced
