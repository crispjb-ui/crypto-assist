"""Accumulation-setup scanner: rally -> deep flush -> basing/early breakout.

The thesis (observed repeatedly on Robinhood Chain runners): initial traction,
then a -60%..-95% drawdown that migrates supply to holders who don't sell;
when demand returns it hits a thin sell side. This module finds tokens in the
post-flush phase from measured price structure, then cross-checks the
diligence ledger so a "flush" that was actually a rug never surfaces.

Price history comes from GeckoTerminal's free public API (no key), which
indexes Robinhood Chain. All thresholds are explicit and tunable — treat them
as hypotheses for the calibration loop, not truths.

    python -m src.onchain.setups [--min-drawdown 60] [--limit 40]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

from . import store

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_NETWORK = os.environ.get("GECKOTERMINAL_NETWORK", "robinhood")
_session = requests.Session()

# Setup filter defaults (percentages as positive numbers).
MIN_DRAWDOWN_PCT = 60.0     # peak-to-trough flush depth to qualify
MIN_RECOVERY_PCT = 15.0     # demand returning off the low...
MAX_RECOVERY_X_ATH = 0.5    # ...but not already back near highs
MIN_LIQUIDITY_USD = 15_000.0
MIN_VOL24_USD = 3_000.0
MIN_AGE_DAYS = 5.0


# GeckoTerminal's free tier allows ~30 calls/min; 2.5s spacing (24/min) stays
# under it. This runs as a background job, so slow-but-complete beats fast-but-
# throttled.
MIN_CALL_GAP_SECONDS = 2.5
_last_call = 0.0


def _get(path: str, params: dict | None = None):
    global _last_call
    for attempt in range(3):
        wait = MIN_CALL_GAP_SECONDS - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        resp = _session.get(f"{GECKO_BASE}{path}", params=params or {},
                            timeout=30, headers={"Accept": "application/json"})
        _last_call = time.time()
        if resp.status_code == 429 and attempt < 2:
            wait_s = 20 * (attempt + 1)
            print(f"note: GeckoTerminal rate limit — pausing {wait_s}s",
                  file=sys.stderr)
            time.sleep(wait_s)
            continue
        resp.raise_for_status()
        return resp.json()
    raise requests.RequestException("GeckoTerminal rate limit persisted")


def top_pools(pages: int = 2) -> list[dict]:
    """Highest-volume pools on the network — the setup universe. Established
    tokens only appear here once they have real volume, which is the point."""
    pools = []
    for page in range(1, pages + 1):
        try:
            body = _get(f"/networks/{GECKO_NETWORK}/pools", {"page": page})
        except Exception as exc:
            print(f"note: GeckoTerminal pools page {page} failed ({exc})",
                  file=sys.stderr)
            break
        pools.extend(body.get("data") or [])
    return pools


def daily_ohlcv(pool_address: str, limit: int = 120) -> list[list[float]]:
    """[[ts, open, high, low, close, volume], ...] ascending by time."""
    body = _get(f"/networks/{GECKO_NETWORK}/pools/{pool_address}/ohlcv/day",
                {"limit": limit})
    rows = (((body.get("data") or {}).get("attributes") or {})
            .get("ohlcv_list") or [])
    return sorted(([float(x) for x in r] for r in rows if len(r) >= 6),
                  key=lambda r: r[0])


def structure(ohlcv: list[list[float]]) -> dict | None:
    """Measure the rally/flush/recovery shape from daily candles."""
    if len(ohlcv) < 5:
        return None
    highs = [(r[0], r[2]) for r in ohlcv]
    ath_ts, ath = max(highs, key=lambda x: x[1])
    post = [r for r in ohlcv if r[0] > ath_ts]
    if not post or ath <= 0:
        return None
    low = min(r[3] for r in post)
    current = ohlcv[-1][4]
    if low <= 0 or current <= 0:
        return None
    return {
        "age_days": (ohlcv[-1][0] - ohlcv[0][0]) / 86400 + 1,
        "ath": ath,
        "drawdown_pct": 100.0 * (ath - low) / ath,
        "recovery_pct": 100.0 * (current - low) / low,
        "current_vs_ath": current / ath,
        "days_since_ath": (ohlcv[-1][0] - ath_ts) / 86400,
    }


def scan(min_drawdown: float = MIN_DRAWDOWN_PCT,
         min_recovery: float = MIN_RECOVERY_PCT,
         limit: int = 40, pages: int = 2) -> list[dict]:
    results = []
    pools = top_pools(pages)
    print(f"screening {len(pools)} pools on '{GECKO_NETWORK}'...", file=sys.stderr)
    for i, pool in enumerate(pools[: limit * 3], 1):
        attrs = pool.get("attributes") or {}
        addr = attrs.get("address") or (pool.get("id") or "").split("_")[-1]
        liq = float(attrs.get("reserve_in_usd") or 0)
        vol24 = float(((attrs.get("volume_usd") or {}).get("h24")) or 0)
        if liq < MIN_LIQUIDITY_USD or vol24 < MIN_VOL24_USD:
            continue
        try:
            s = structure(daily_ohlcv(addr))
        except Exception:
            continue
        if not s or s["age_days"] < MIN_AGE_DAYS:
            continue
        if s["drawdown_pct"] < min_drawdown:
            continue
        if not (min_recovery <= s["recovery_pct"]
                and s["current_vs_ath"] <= MAX_RECOVERY_X_ATH):
            continue
        base = (attrs.get("name") or "?").split(" / ")[0]
        token_addr = ((((pool.get("relationships") or {}).get("base_token") or {})
                       .get("data") or {}).get("id") or "")
        token_addr = token_addr.split("_")[-1] if token_addr else None
        results.append({
            "symbol": base, "pool": addr, "token": token_addr,
            "liquidity_usd": round(liq), "vol24_usd": round(vol24),
            **{k: round(v, 2) for k, v in s.items()},
        })
        if len(results) >= limit:
            break
        if i % 20 == 0:
            print(f"  screened {i} pools, {len(results)} setups", file=sys.stderr)

    # cross-check the diligence ledger: a known-bad token is not a setup
    tokens = [r["token"] for r in results if r["token"]]
    scores = store.latest_scores(tokens) if tokens else {}
    for r in results:
        r["diligence_score"] = scores.get(r["token"] or "", None)
    results = [r for r in results
               if r["diligence_score"] is None or r["diligence_score"] <= 2]
    results.sort(key=lambda r: (-r["drawdown_pct"], r["recovery_pct"]))
    return results


def check_token(token: str) -> dict:
    """Measure one token's structure against the setup bar (e.g. $PARE)."""
    body = _get(f"/networks/{GECKO_NETWORK}/tokens/{token.lower()}/pools",
                {"page": 1})
    pools = body.get("data") or []
    if not pools:
        return {"token": token, "error": "no pools on GeckoTerminal for this token"}
    best = max(pools, key=lambda p: float((p.get("attributes") or {})
                                          .get("reserve_in_usd") or 0))
    attrs = best.get("attributes") or {}
    addr = attrs.get("address") or (best.get("id") or "").split("_")[-1]
    s = structure(daily_ohlcv(addr))
    if not s:
        return {"token": token, "pool": addr,
                "error": "not enough daily history to measure structure"}
    qualifies = (s["drawdown_pct"] >= MIN_DRAWDOWN_PCT
                 and s["recovery_pct"] >= MIN_RECOVERY_PCT
                 and s["current_vs_ath"] <= MAX_RECOVERY_X_ATH
                 and s["age_days"] >= MIN_AGE_DAYS)
    score = store.latest_scores([token]).get(token.lower())
    return {"token": token, "pool": addr,
            "symbol": (attrs.get("name") or "?").split(" / ")[0],
            "liquidity_usd": round(float(attrs.get("reserve_in_usd") or 0)),
            **{k: round(v, 2) for k, v in s.items()},
            "diligence_score": score, "qualifies": qualifies}


def main() -> None:
    ap = argparse.ArgumentParser(description="Accumulation-setup scanner")
    ap.add_argument("--min-drawdown", type=float, default=MIN_DRAWDOWN_PCT)
    ap.add_argument("--min-recovery", type=float, default=MIN_RECOVERY_PCT)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--token", help="measure one token's structure instead of "
                                    "scanning the whole network")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.token:
        print(json.dumps(check_token(args.token), indent=2))
        return
    rows = scan(args.min_drawdown, args.min_recovery, args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No setups matched. Normal outcome — the pattern is rare by "
              "definition; loosen --min-drawdown/--min-recovery to explore.")
        return
    print(f"{'symbol':>10}  {'drawdown':>9} {'recovery':>9} {'vsATH':>6} "
          f"{'liq$':>10} {'score':>6}  token")
    for r in rows:
        score = r["diligence_score"]
        print(f"{r['symbol']:>10.10}  {r['drawdown_pct']:>8.1f}% "
              f"{r['recovery_pct']:>8.1f}% {r['current_vs_ath']:>6.2f} "
              f"{r['liquidity_usd']:>10,} "
              f"{score if score is not None else '—':>6}  {r['token'] or r['pool']}")
    print("\nSetups are price structure + a clean-or-unchecked ledger — run "
          "/diligence on each before acting; 'supply migrated to strong hands' "
          "still needs the holder check, and no output here is a buy signal.")


if __name__ == "__main__":
    main()
