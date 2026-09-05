"""Unattended daily pipeline — run from a scheduler (Windows Task Scheduler,
cron). Keeps the improvement flywheel turning without a human in the loop:

  1. Label outcomes for yesterday's diligence runs (outcomes update).
  2. Scan Pons and Long launches.
  3. Auto-run full diligence on a sample of launches (the most-sniped plus a
     few with none, so the ledger learns both classes) — each run is logged
     and will be labeled by tomorrow's step 1.
  4. Print flag-precision stats.

The one step that stays human+Claude: acting on the stats (/improve-detectors
changes code and requires review). Everything else is deterministic.

    python -m src.onchain.daily [--pons-hours 24] [--long-hours 24] [--reports 8]
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback

from . import long as long_mod
from . import outcomes, pons, report, store
from .pons import block_near_time
from .rpc import EvmRpc


def _stage(name: str):
    print(f"\n=== {name} — {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)


def print_shortlist(pons_launches, long_launches, since_ts: int) -> None:
    """Positive selection from measured evidence only. A listing here means the
    rig-checks passed AND organic-demand markers are present — it is a
    watchlist for fundamental research, never a buy signal.

    Ranked by organic demand (taxed outside buys on Pons, distinct early
    buyers on Long) among launches with no insider snipe activity and a low
    diligence score where one was recorded today.
    """
    scores: dict[str, int] = {}
    with store.connect() as conn:
        for token, score in conn.execute(
                "SELECT token, score FROM runs WHERE ts >= ?", (since_ts,)):
            scores[token] = min(score, scores.get(token, 99))

    rows = []
    for l in pons_launches:
        if l.exempt_buys or l.declared_exemptions:
            continue                      # insider cluster present
        if not l.taxed_buys:
            continue                      # no measured outside demand
        score = scores.get(l.token)
        if score is not None and score > 2:
            continue                      # diligence found real flags
        rows.append(("pons", l.symbol, l.token, l.taxed_buys,
                     l.graduated, score))
    for l in long_launches:
        if not l.early_buys or l.offloaded_top >= 3:
            continue
        score = scores.get(l.token)
        if score is not None and score > 2:
            continue
        rows.append((f"long/{l.paired_symbol}", l.symbol, l.token,
                     l.early_buyers, False, score))

    rows.sort(key=lambda r: (-int(r[4]), -r[3]))
    if not rows:
        print("No launches met the candidate bar today (that is the normal "
              "outcome — most days produce nothing worth your attention).")
        return
    print("Rig-checks passed + organic demand measured. NOT buy signals — "
          "this is the research queue:")
    for venue, symbol, token, demand, graduated, score in rows[:10]:
        tag = "GRADUATED  " if graduated else ""
        scored = f"score {score}" if score is not None else "no deep check yet"
        print(f"  {venue:12} {symbol:10.10} {token}  organic-demand {demand:>4}  "
              f"{tag}{scored}")
    print("Next for each: verify contract source yourself, re-check holder "
          "drift in 24-72h (re-run report), and judge team/product off-chain "
          "— nothing on this list has been vetted for any of that.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Unattended daily diligence pipeline")
    ap.add_argument("--pons-hours", type=float, default=24)
    ap.add_argument("--long-hours", type=float, default=24)
    ap.add_argument("--reports", type=int, default=8,
                    help="max auto-diligence reports per run")
    args = ap.parse_args()
    failures = 0
    run_started = int(time.time())

    _stage("outcomes update")
    try:
        outcomes.cmd_update(min_age_hours=20.0)
    except Exception:
        traceback.print_exc()
        failures += 1

    rpc = EvmRpc()
    to_block = rpc.latest_block()
    snipe_window = rpc.blocks_for_seconds(30, floor=100, cap=2_000)

    pons_launches = []
    _stage(f"pons scan ({args.pons_hours}h)")
    try:
        from_block = block_near_time(rpc, int(time.time() - args.pons_hours * 3600))
        pons_launches = pons.recent_launches(
            rpc, from_block, to_block, deep=True, limit=100,
            snipe_window_blocks=snipe_window)
        print(f"{len(pons_launches)} Pons launches analyzed")
    except Exception:
        traceback.print_exc()
        failures += 1

    long_launches = []
    _stage(f"long scan ({args.long_hours}h)")
    try:
        from_block = block_near_time(rpc, int(time.time() - args.long_hours * 3600))
        window = rpc.blocks_for_seconds(300, floor=200, cap=50_000)
        long_launches = long_mod.recent_launches(
            rpc, from_block, to_block, deep=True, limit=50, window_blocks=window)
        print(f"{len(long_launches)} Long launches analyzed")
    except Exception:
        traceback.print_exc()
        failures += 1

    _stage("auto-diligence sample")
    # Most snipe activity first (likely farms), then quiet ones (controls):
    # the calibration loop needs labeled examples of BOTH classes.
    hot = sorted((l for l in pons_launches if l.exempt_buys),
                 key=lambda l: -l.exempt_buy_quote)
    quiet = [l for l in pons_launches if not l.exempt_buys and l.taxed_buys]
    picks = (hot[: args.reports // 2] + quiet[: args.reports - len(hot[: args.reports // 2])])
    hot_long = sorted((l for l in long_launches if l.early_buys),
                      key=lambda l: -l.early_buys)[:2]
    for launch in picks:
        try:
            print(f"\n--- report {launch.symbol} {launch.token} ---")
            report.run(launch.token, launch.curve_or_pool,
                       bundle_only=False, as_json=False)
        except SystemExit:
            pass
        except Exception:
            traceback.print_exc()
            failures += 1
    for launch in hot_long:
        try:
            print(f"\n--- report (Long) {launch.symbol} {launch.token} ---")
            report.run(launch.token, long_mod.POOL_MANAGER,
                       bundle_only=False, as_json=False,
                       creation_block=launch.block)
        except SystemExit:
            pass
        except Exception:
            traceback.print_exc()
            failures += 1

    _stage("wallet-intelligence ingest")
    try:
        from . import wallets
        wallets.scan(hours=args.pons_hours, limit=120)
    except Exception:
        traceback.print_exc()
        failures += 1

    _stage("candidate shortlist")
    try:
        print_shortlist(pons_launches, long_launches, run_started)
    except Exception:
        traceback.print_exc()
        failures += 1

    _stage("flag calibration stats")
    try:
        outcomes.cmd_stats()
    except Exception:
        traceback.print_exc()
        failures += 1

    _stage(f"done — {failures} stage failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
