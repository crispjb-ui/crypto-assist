"""Social forensics for shortlisted tokens.

Two layers, each degrading gracefully:
  1. FREE — token's declared social links via DexScreener (no key).
  2. X API (X_BEARER_TOKEN in .env; paid tier required for search) — account
     forensics on the token's X handle: created-at vs token launch, followers,
     posting cadence; plus recent cashtag mentions when search is available.

Scope note: this measures astroturf signals (the farm pattern: fresh account,
few posts, no organic engagement). It does NOT compute a sentiment score —
raw mentions are fetched so a human or a Claude session can read them; a
keyword-polarity number would be false precision.

    python -m src.onchain.social <TOKEN_ADDRESS> [--symbol XYZ]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

from . import config, dexscreener

X_API = "https://api.x.com/2"
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
_session = requests.Session()


def token_socials(token: str) -> dict:
    """Declared links from DexScreener pair info (free, unauthenticated)."""
    out = {"twitter": None, "telegram": None, "websites": [], "source": None}
    try:
        pair = dexscreener.best_pair_for_token(token, config.DEXSCREENER_CHAIN_ID or None)
    except Exception as exc:
        out["source"] = f"dexscreener unavailable: {exc}"
        return out
    info = (pair or {}).get("info") or {}
    for social in info.get("socials") or []:
        stype = (social.get("type") or "").lower()
        url = social.get("url") or ""
        if stype == "twitter" or "x.com" in url or "twitter.com" in url:
            out["twitter"] = url
        elif stype == "telegram":
            out["telegram"] = url
    out["websites"] = [w.get("url") for w in info.get("websites") or [] if w.get("url")]
    out["source"] = "dexscreener"
    return out


def _x_get(path: str, params: dict) -> dict | None:
    if not X_BEARER_TOKEN:
        return None
    try:
        resp = _session.get(f"{X_API}{path}", params=params, timeout=30,
                            headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"})
        if resp.status_code == 429:
            print("note: X API rate-limited; skipping", file=sys.stderr)
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        print(f"note: X API call failed ({exc})", file=sys.stderr)
        return None


def handle_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:x|twitter)\.com/(@?[A-Za-z0-9_]{1,15})", url)
    if not m:
        return None
    handle = m.group(1).lstrip("@")
    return None if handle.lower() in {"search", "intent", "share", "i"} else handle


def account_forensics(handle: str, launch_ts: int | None) -> dict:
    """Astroturf signals on the token's X account. None fields = not measurable."""
    body = _x_get(f"/users/by/username/{handle}",
                  {"user.fields": "created_at,public_metrics"})
    if not body or "data" not in body:
        return {"handle": handle, "available": False}
    data = body["data"]
    metrics = data.get("public_metrics") or {}
    created_at = data.get("created_at")
    created_ts = None
    if created_at:
        created_ts = int(datetime.fromisoformat(
            created_at.replace("Z", "+00:00")).timestamp())
    result = {
        "handle": handle,
        "available": True,
        "account_created": created_at,
        "followers": metrics.get("followers_count"),
        "following": metrics.get("following_count"),
        "tweet_count": metrics.get("tweet_count"),
        "account_age_days": round((time.time() - created_ts) / 86400, 1)
                            if created_ts else None,
        "created_after_token_launch": (created_ts > launch_ts)
                                      if created_ts and launch_ts else None,
    }
    return result


def recent_mentions(query: str, max_results: int = 25) -> list[dict]:
    """Raw recent posts mentioning the token (requires paid-tier search).
    Returned verbatim for a human/Claude to read — no polarity scoring here."""
    body = _x_get("/tweets/search/recent",
                  {"query": f"{query} -is:retweet", "max_results": max_results,
                   "tweet.fields": "created_at,public_metrics,author_id"})
    return (body or {}).get("data") or []


def check(token: str, symbol: str | None = None,
          launch_ts: int | None = None) -> dict:
    socials = token_socials(token)
    report: dict = {"token": token.lower(), "socials": socials}
    handle = handle_from_url(socials.get("twitter"))
    if not handle:
        report["x"] = {"available": False,
                       "reason": "no X link declared on DexScreener"}
        return report
    if not X_BEARER_TOKEN:
        report["x"] = {"available": False, "handle": handle,
                       "reason": "X_BEARER_TOKEN not set — account forensics "
                                 "and mentions skipped"}
        return report
    report["x"] = account_forensics(handle, launch_ts)
    terms = [f"@{handle}"]
    if symbol and symbol not in ("?", ""):
        terms.append(f"${symbol}")
    report["mentions"] = recent_mentions(" OR ".join(terms))
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Social forensics for a token")
    ap.add_argument("token")
    ap.add_argument("--symbol", help="cashtag to include in mention search")
    ap.add_argument("--launch-ts", type=int,
                    help="unix launch time, to flag accounts created after it")
    args = ap.parse_args()
    print(json.dumps(check(args.token, args.symbol, args.launch_ts), indent=2))


if __name__ == "__main__":
    main()
