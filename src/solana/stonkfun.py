"""stonk.fun launchpad watcher — bonding-phase and about-to-graduate tokens.

The launchpad's own API (reported as
https://www.stonkfun.xyz/api/public/v1/tokens) sees tokens BEFORE they have a
real pool, which GeckoTerminal cannot. That endpoint, its parameters, and its
field names are UNVERIFIED from the development environment (crypto domains
blocked) — so this client is schema-tolerant: it discovers record shape at
runtime, maps fields by candidate names, and refuses to invent values it
cannot find. Verify the API on your machine first:

    python -m src.solana.stonkfun probe     # status, shape, sample record
    python -m src.solana.stonkfun top       # ranked shortlist in terminal

Feed integration is fail-quiet: if the API is absent or its shape changes,
the dashboard's GeckoTerminal path continues untouched.

Scoring thresholds below are DECLARED HYPOTHESES (from observed leaderboard
behavior reported by the user, not measured here): calibrate them against the
ledger like every other detector, never treat them as facts.
"""
from __future__ import annotations

import os
import sys
import time

import requests

from ..onchain import store

DEFAULT_API = "https://www.stonkfun.xyz/api/public/v1/tokens"
SOURCE = "stonkfun"
_HEADERS = {"Accept": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36")}
_session = requests.Session()

# --- hypothesis thresholds (tune via calibration, or env overrides) ---
MIN_VOL_MCAP_RATIO = float(os.environ.get("STONK_MIN_VOL_MCAP", "0.3"))
MIN_MCAP_USD = float(os.environ.get("STONK_MIN_MCAP", "5000"))
SERIAL_CREATOR_MIN = int(os.environ.get("STONK_SERIAL_CREATOR", "4"))

_POLL_TTL = 90.0
_cache: dict = {"ts": 0.0, "rows": None}


def api_url() -> str:
    return os.environ.get("STONKFUN_API_URL", DEFAULT_API).rstrip("/")


def _get(params: dict) -> tuple[int, object]:
    resp = _session.get(api_url(), params=params, headers=_HEADERS, timeout=20)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:400]
    return resp.status_code, body


def _records(body) -> list[dict]:
    """Find the token array wherever the API nests it."""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        for key in ("tokens", "data", "items", "results", "records"):
            v = body.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):   # one more nesting level (data.tokens)
                inner = _records(v)
                if inner:
                    return inner
    return []


def _field(rec: dict, *names, default=None):
    for n in names:
        if n in rec and rec[n] is not None:
            return rec[n]
    return default


def _num(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def normalize(rec: dict) -> dict | None:
    """Map one API record onto known fields; None when no mint is found.
    Missing values stay None — assessment treats them as unmeasured."""
    mint = _field(rec, "mint", "address", "tokenAddress", "token_address",
                  "ca", "id")
    if not isinstance(mint, str) or not (32 <= len(mint) <= 44):
        return None
    return {
        "mint": mint,
        "symbol": _field(rec, "symbol", "ticker", "name", default="?"),
        "name": _field(rec, "name", "symbol", default="?"),
        "quote": _field(rec, "quoteSymbol", "quote_symbol", "quoteToken",
                        "quote", "pairedWith", default="?"),
        "mcap": _num(_field(rec, "marketCap", "market_cap", "mcap",
                            "usdMarketCap", "usd_market_cap")),
        "volume24h": _num(_field(rec, "volume24h", "volume_24h", "volume",
                                 "volumeUsd", "volume_usd")),
        "creator": _field(rec, "creator", "creatorWallet", "creator_wallet",
                          "creatorAddress", "deployer"),
        "status": _field(rec, "status", "state", "phase"),
        "created_at": _field(rec, "createdAt", "created_at", "launchedAt",
                             "launched_at", "timestamp"),
        "holders": _num(_field(rec, "holders", "holderCount", "holder_count")),
    }


def poll() -> list[dict]:
    """Newest + about-to-graduate tokens, deduped, newest first. Cached ~90s
    so the scanner loop doesn't hammer the launchpad."""
    if _cache["rows"] is not None and time.time() - _cache["ts"] < _POLL_TTL:
        return _cache["rows"]
    merged: dict[str, dict] = {}
    for params in ({"sort": "newest", "pageSize": 100},
                   {"status": "aboutToGraduate", "sort": "volume"}):
        try:
            status, body = _get(params)
        except requests.RequestException as exc:
            print(f"stonkfun: request failed ({exc})", file=sys.stderr)
            continue
        if status != 200:
            print(f"stonkfun: HTTP {status} for {params} — run "
                  "'python -m src.solana.stonkfun probe' to inspect",
                  file=sys.stderr)
            continue
        for rec in _records(body):
            row = normalize(rec)
            if row:
                if params.get("status") == "aboutToGraduate":
                    row["graduating"] = True
                merged.setdefault(row["mint"], {}).update(row)
    rows = list(merged.values())
    if rows:
        _cache.update(ts=time.time(), rows=rows)
        for r in rows:      # serial-launcher memory grows with every poll
            if r.get("creator"):
                try:
                    store.record_launch_creator(SOURCE, r["mint"], r["creator"])
                except Exception:
                    pass
    return rows


def assess(row: dict, creator_counts: dict[str, int]) -> tuple[float, str, bool]:
    """(opportunity score, reason, hot). Only measured fields contribute;
    unmeasured fields are named, never assumed good or bad."""
    score, reasons, missing = 0.0, [], []
    mcap, vol = row.get("mcap"), row.get("volume24h")

    if mcap is None:
        missing.append("mcap")
    elif mcap < MIN_MCAP_USD:
        score -= 4
        reasons.append(f"mcap ${mcap:,.0f} < ${MIN_MCAP_USD:,.0f}")
    if vol is not None and mcap and mcap > 0:
        ratio = vol / mcap
        if ratio >= MIN_VOL_MCAP_RATIO:
            score += min(ratio, 3.0) * 4
            reasons.append(f"vol/mcap {ratio:.2f}")
    elif vol is None:
        missing.append("volume")

    if row.get("graduating"):
        score += 6
        reasons.append("about to graduate")

    creator = row.get("creator")
    if creator:
        n = creator_counts.get(store._key(creator), 0)
        if n >= SERIAL_CREATOR_MIN:
            score -= 3
            reasons.append(f"serial creator ({n} launches)")
        elif n <= 1:
            score += 1
            reasons.append("first-time creator")
    else:
        missing.append("creator")

    if missing:
        reasons.append("unmeasured: " + ",".join(missing))
    hot = (score >= 6 and not any("serial creator" in r for r in reasons)
           and (mcap or 0) >= MIN_MCAP_USD)
    return score, ", ".join(reasons) or "no signals", hot


def feed_rows(opportunity_unused=None) -> dict[str, dict]:
    """Dashboard rows for bonding-phase stonk.fun tokens. Deep on-chain
    analysis arrives via the regular solana feed once a pool exists; these
    rows are the earlier radar."""
    counts = {}
    try:
        counts = store.creator_launch_counts(SOURCE)
    except Exception:
        pass
    rows: dict[str, dict] = {}
    for r in poll():
        opp, reason, hot = assess(r, counts)
        cls = "watch" if hot else ("quiet" if opp >= 0 else "bad")
        rows[r["mint"]] = {
            "venue": "sol/stonk.fun", "symbol": str(r.get("symbol") or "?"),
            "token": r["mint"], "pair": "",
            "block": 0, "creation_block": 0,
            "cls": cls, "graduated": bool(r.get("graduating")),
            "smart": 0, "setup": False, "hot": hot,
            "opp": round(opp, 1), "reason": reason,
            "detail": (f"quote {r.get('quote')}, "
                       f"mcap ${(r.get('mcap') or 0):,.0f}, "
                       f"24h vol ${(r.get('volume24h') or 0):,.0f}"
                       + (f", {int(r['holders'])} holders"
                          if r.get("holders") is not None else "")
                       + (f", status {r['status']}" if r.get("status") else "")),
        }
    return rows


def probe() -> int:
    """Verify the API exists and show its real shape — run on your machine."""
    for params in ({"sort": "newest", "pageSize": 5},
                   {"status": "aboutToGraduate", "sort": "volume"}):
        print(f"\nGET {api_url()} {params}")
        try:
            status, body = _get(params)
        except requests.RequestException as exc:
            print(f"  request failed: {exc}")
            continue
        print(f"  HTTP {status}")
        if isinstance(body, dict):
            print(f"  top-level keys: {sorted(body.keys())}")
        recs = _records(body)
        print(f"  records found: {len(recs)}")
        if recs:
            print(f"  first record keys: {sorted(recs[0].keys())}")
            norm = normalize(recs[0])
            print(f"  normalized: {norm}")
            unmapped = None if norm else "NO MINT FIELD RECOGNIZED"
            if unmapped:
                print(f"  !! {unmapped} — paste this output to Claude")
        elif status == 200:
            print(f"  body sample: {str(body)[:300]}")
            print("  !! no token array recognized — paste this output to Claude")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"
    if cmd == "probe":
        return probe()
    counts = store.creator_launch_counts(SOURCE)
    rows = poll()
    scored = sorted((assess(r, counts) + (r,) for r in rows),
                    key=lambda t: -t[0])
    print(f"stonk.fun — {len(rows)} tokens, best first "
          "(hypothesis scores, not buy signals):")
    for opp, reason, hot, r in scored[:25]:
        print(f"{'HOT ' if hot else '    '}{opp:6.1f}  "
              f"{str(r.get('symbol') or '?'):10.10s} {r['mint']}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
