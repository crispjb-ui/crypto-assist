"""Solana live launch feed — new pools from GeckoTerminal, launch-window
classification on-chain, wallet-PnL ingest, smart/cohort cross-reference.

Runs inside the dashboard's scanner loop (guarded: does nothing without
SOLANA_RPC_URL) and returns rows shaped like the EVM feed's, so ranking,
candidates-only filtering, deep-scan-score demotion, and desktop alerts all
work unchanged. Each newly seen pool is analyzed once (launch window +
per-wallet quote flows from the same transactions); rescans are free.
"""
from __future__ import annotations

import os
import sys
import time

from ..onchain import setups, store, wallets
from . import b58
from .report import CHAIN_KEY, analyze_launch
from .rpc import SolRpc

MAX_NEW_POOLS_PER_CYCLE = 8       # deep launch analysis budget per cycle
MAX_POOL_AGE_MINUTES = 90         # older pools aren't "launches" anymore
_analyzed: dict[str, dict] = {}   # token -> cached row core (per process)
_ANALYZED_CAP = 500


def _iso_to_ts(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        return int(time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0


def new_pools() -> list[dict]:
    """Newest Solana pools from GeckoTerminal (shares the setups client's
    throttle/backoff and API key)."""
    body = setups._get("/networks/solana/new_pools", {"page": 1})
    return body.get("data") or []


def _classify(launch, smart_hits: int, cohort_hits: int) -> str:
    if cohort_hits:
        return "bad"
    if launch.creation_slot_buyers >= 3:
        return "bad"
    if launch.buyers:
        return "watch"
    return "quiet"


def scan(opportunity_fn, enabled: bool | None = None) -> dict[str, dict]:
    """One feed pass. `opportunity_fn` is server.opportunity (passed in to
    avoid a circular import). Returns {token: row}."""
    if enabled is None:
        enabled = bool(os.environ.get("SOLANA_RPC_URL"))
    if not enabled:
        return {}

    try:
        pools = new_pools()
    except Exception as exc:
        print(f"solana feed: new_pools failed ({exc})", file=sys.stderr)
        return {}

    try:
        smart = wallets.smart_set(chain=CHAIN_KEY)
        cohort = wallets.cohort_set(chain=CHAIN_KEY)
    except Exception:
        smart, cohort = set(), set()

    rpc = None
    rows: dict[str, dict] = {}
    analyzed_this_cycle = 0
    now = time.time()
    for p in pools:
        attrs = p.get("attributes") or {}
        rel = p.get("relationships") or {}
        pool = (attrs.get("address") or "").strip()
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        token = base_id.partition("_")[2]
        quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "")
        quote_mint = quote_id.partition("_")[2]
        if not (pool and b58.looks_like_address(token)):
            continue
        created = _iso_to_ts(attrs.get("pool_created_at"))
        if created and now - created > MAX_POOL_AGE_MINUTES * 60:
            continue

        cached = _analyzed.get(token)
        if cached is None:
            if analyzed_this_cycle >= MAX_NEW_POOLS_PER_CYCLE:
                continue
            analyzed_this_cycle += 1
            try:
                rpc = rpc or SolRpc()
                launch = analyze_launch(rpc, token, pool, quote_mint or None)
            except Exception as exc:
                print(f"solana feed: {token} analysis failed ({exc})",
                      file=sys.stderr)
                continue
            quote_sym = (attrs.get("name") or "/").split("/")[-1].strip() or "?"
            if quote_mint:
                store.remember_quote_token(quote_sym, quote_mint,
                                           chain=CHAIN_KEY)
            # ingest per-wallet quote flows measured during the same analysis
            try:
                if launch.wallet_trades and \
                        not store.wallet_scan_done(token, chain=CHAIN_KEY):
                    for w, (spent, recv, n) in launch.wallet_trades.items():
                        store.upsert_wallet_trade(w, token, spent, recv, n,
                                                  quote_symbol=quote_sym,
                                                  chain=CHAIN_KEY)
                    store.mark_wallet_scan(token, chain=CHAIN_KEY)
            except Exception as exc:
                print(f"solana feed: wallet ingest failed ({exc})",
                      file=sys.stderr)
            cached = {
                "launch": launch,
                "symbol": (attrs.get("name") or "?").split("/")[0].strip(),
                "venue": f"sol/{launch.venue}",
                "slot": launch.creation_slot,
            }
            _analyzed[token] = cached
            while len(_analyzed) > _ANALYZED_CAP:
                _analyzed.pop(next(iter(_analyzed)))

        launch = cached["launch"]
        buyer_wallets = {b.wallet for b in launch.buyers}
        smart_hits = smart & buyer_wallets
        cohort_hits = cohort & buyer_wallets
        cls = _classify(launch, len(smart_hits), len(cohort_hits))
        if smart_hits and cls == "quiet":
            cls = "watch"
        insider = launch.creation_slot_buyers + len(cohort_hits)
        opp, reason = opportunity_fn(len(launch.buyers), len(smart_hits),
                                     False, insider, 0, False)
        if cohort_hits:
            reason += f", OPERATOR COHORT x{len(cohort_hits)}"
        if not launch.history_complete:
            reason += ", launch window unresolved"
        rows[token] = {
            "venue": cached["venue"], "symbol": cached["symbol"],
            "token": token, "pair": launch.pool,
            "block": cached["slot"], "creation_block": cached["slot"],
            "cls": cls, "graduated": False,
            "smart": len(smart_hits), "setup": False,
            "opp": round(opp, 1), "reason": reason,
            "detail": (f"{launch.creation_slot_buyers} creation-slot buys, "
                       f"{len(launch.buyers)} early buyers, "
                       f"{launch.sells_in_window} sells in window"
                       + (f" — SMART MONEY x{len(smart_hits)}"
                          if smart_hits else "")),
        }
    return rows


def trace_smart_funders(limit: int = 100) -> int:
    """Funding-trace solana smart candidates missing one (cohort evidence).
    Runs inside the periodic wallet job; needs SOLANA_RPC_URL."""
    if not os.environ.get("SOLANA_RPC_URL"):
        return 0
    from .report import _inbound_funder
    have = store.wallet_funder_map(chain=CHAIN_KEY)
    todo = [w for w in sorted(wallets.smart_candidates(chain=CHAIN_KEY))
            if w not in have][:limit]
    if not todo:
        return 0
    rpc = SolRpc()
    traced = 0
    for w in todo:
        try:
            page = rpc.signatures(w, limit=1000)
            if len(page) >= 1000:
                continue                  # deep history; earliest unreachable
            earliest = page[-1]["signature"] if page else None
            funder = None
            if earliest:
                tx = rpc.transactions([earliest])[0]
                if tx:
                    funder = _inbound_funder(tx, w)
            store.remember_wallet_funder(w, funder or "", chain=CHAIN_KEY)
            traced += 1
        except Exception:
            continue
    return traced
