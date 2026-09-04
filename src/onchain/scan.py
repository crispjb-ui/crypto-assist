"""Launch scanner: pull DexScreener discovery feeds, filter to young pairs on the
target chain, and pre-rank them for deep diligence.

    python -m src.onchain.scan --max-age-hours 24 --min-liquidity-usd 20000
"""
from __future__ import annotations

import argparse
import sys
import time

from . import config, dexscreener


def scan(chain: str, max_age_hours: float, min_liquidity: float,
         min_volume: float) -> list[dict]:
    seen: set[str] = set()
    candidates: list[dict] = []
    boosted_addrs: set[str] = set()

    try:
        boosted = dexscreener.latest_boosted()
        boosted_addrs = {b.get("tokenAddress", "").lower()
                         for b in boosted if b.get("chainId") == chain}
    except Exception:
        pass

    profiles = dexscreener.latest_token_profiles()
    now_ms = time.time() * 1000

    for prof in profiles:
        if prof.get("chainId") != chain:
            continue
        addr = (prof.get("tokenAddress") or "").lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        try:
            best = dexscreener.best_pair_for_token(addr, chain)
        except Exception:
            continue
        if not best:
            continue
        created = best.get("pairCreatedAt") or 0
        age_h = (now_ms - created) / 3_600_000 if created else None
        liq = (best.get("liquidity") or {}).get("usd") or 0
        vol24 = (best.get("volume") or {}).get("h24") or 0
        if age_h is not None and age_h > max_age_hours:
            continue
        if liq < min_liquidity or vol24 < min_volume:
            continue
        candidates.append({
            "token": addr,
            "symbol": (best.get("baseToken") or {}).get("symbol", "?"),
            "pair": best.get("pairAddress"),
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "liquidity_usd": round(liq),
            "volume24_usd": round(vol24),
            "price_usd": best.get("priceUsd"),
            "paying_for_boosts": addr in boosted_addrs,
            "url": best.get("url"),
        })

    candidates.sort(key=lambda c: -(c["volume24_usd"] / max(c["liquidity_usd"], 1)))
    return candidates


def main() -> None:
    ap = argparse.ArgumentParser(description="Scan for young pairs worth diligence")
    ap.add_argument("--chain", default=config.DEXSCREENER_CHAIN_ID,
                    help="DexScreener chain slug (default: DEXSCREENER_CHAIN_ID)")
    ap.add_argument("--max-age-hours", type=float, default=24)
    ap.add_argument("--min-liquidity-usd", type=float, default=10_000)
    ap.add_argument("--min-volume-usd", type=float, default=5_000)
    args = ap.parse_args()

    if not args.chain:
        print("Set DEXSCREENER_CHAIN_ID in .env or pass --chain. If DexScreener "
              "does not index your chain yet, discovery must come from watching "
              "the DEX factory's pool-creation events instead (see README).",
              file=sys.stderr)
        sys.exit(2)

    results = scan(args.chain, args.max_age_hours,
                   args.min_liquidity_usd, args.min_volume_usd)
    if not results:
        print("No candidates matched the filters.")
        return
    for c in results:
        boost = "  [PAYING FOR BOOSTS]" if c["paying_for_boosts"] else ""
        print(f"{c['symbol']:>10}  {c['token']}  age {c['age_hours']}h  "
              f"liq ${c['liquidity_usd']:,}  vol24 ${c['volume24_usd']:,}{boost}")
    print(f"\n{len(results)} candidate(s). Deep-check each: "
          f"python -m src.onchain.report <token> --pair <pair>")


if __name__ == "__main__":
    main()
