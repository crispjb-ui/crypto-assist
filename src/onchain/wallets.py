"""Smart-wallet intelligence: measured per-wallet PnL on Pons curve trades.

The inversion of the farm detectors: instead of only flagging bad clusters,
track which wallets consistently take profit out of launches. Pons curves emit
CurveBuy/CurveSell with exact quote amounts, so realized PnL per wallet per
token is computed from chain data — no leaderboard API, no claims.

Realized PnL = quote received from sells − quote spent on buys, per (wallet,
token). Unsold inventory is ignored (conservative: winners still holding look
worse, never better). Accumulates in data/diligence.db across scans;
idempotent per launch.

    python -m src.onchain.wallets scan --hours 24    # ingest recent launches
    python -m src.onchain.wallets top                # leaderboard
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from . import pons, store
from .pons import TOPIC_CURVE_BUY, TOPIC_CURVE_SELL, block_near_time
from .rpc import EvmRpc

import time

# A wallet qualifies as "smart money" only with a real sample and a real edge.
SMART_MIN_TOKENS = 4
SMART_MIN_WIN_RATE = 0.6
SMART_MIN_REALIZED_QUOTE = 0.5  # net quote units (ETH) actually extracted


def ingest_curve_trades(rpc: EvmRpc, token: str, curve: str,
                        launch_block: int, window_blocks: int) -> int:
    """Aggregate one launch's curve trades into the wallet ledger."""
    logs = rpc.get_logs(launch_block, launch_block + window_blocks,
                        address=curve, topics=[[TOPIC_CURVE_BUY, TOPIC_CURVE_SELL]])
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])  # spent, recv, n
    for log in logs:
        topics = log.get("topics") or []
        data = log.get("data", "0x")[2:]
        if len(topics) < 2 or len(data) < 128:
            continue
        wallet = "0x" + topics[1][-40:]
        if topics[0] == TOPIC_CURVE_BUY:
            agg[wallet][0] += int(data[0:64], 16) / 1e18     # quoteIn
        elif topics[0] == TOPIC_CURVE_SELL:
            agg[wallet][1] += int(data[64:128], 16) / 1e18   # quoteOut
        else:
            continue
        agg[wallet][2] += 1
    for wallet, (spent, received, trades) in agg.items():
        store.upsert_wallet_trade(wallet, token, spent, received, trades)
    return len(logs)


def scan(hours: float, limit: int, activity_hours: float = 2.0) -> None:
    rpc = EvmRpc()
    to_block = rpc.latest_block()
    from_block = block_near_time(rpc, int(time.time() - hours * 3600))
    window = rpc.blocks_for_seconds(activity_hours * 3600, floor=500, cap=100_000)
    launches = pons.recent_launches(rpc, from_block, to_block, deep=False,
                                    limit=limit)
    new = 0
    for i, l in enumerate(launches, 1):
        if l.version != 2 or store.wallet_scan_done(l.token):
            continue
        n = ingest_curve_trades(rpc, l.token, l.curve_or_pool, l.block, window)
        store.mark_wallet_scan(l.token)
        new += 1
        print(f"  [{i}/{len(launches)}] {l.token} — {n} trade events",
              file=sys.stderr)
    print(f"ingested {new} new launch(es) into the wallet ledger")


def leaderboard(min_tokens: int = SMART_MIN_TOKENS) -> list[dict]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT wallet, COUNT(*) AS tokens, "
            "SUM(received - spent) AS realized, "
            "SUM(CASE WHEN received > spent THEN 1 ELSE 0 END) AS wins, "
            "SUM(trades) AS trades "
            "FROM wallet_trades GROUP BY wallet HAVING tokens >= ? "
            "ORDER BY realized DESC", (min_tokens,),
        ).fetchall()
    return [{"wallet": w, "tokens": t, "realized_quote": r or 0.0,
             "wins": wi, "win_rate": wi / t if t else 0.0, "trades": tr}
            for (w, t, r, wi, tr) in rows]


def smart_set(min_tokens: int = SMART_MIN_TOKENS,
              min_win_rate: float = SMART_MIN_WIN_RATE,
              min_realized: float = SMART_MIN_REALIZED_QUOTE) -> set[str]:
    return {row["wallet"] for row in leaderboard(min_tokens)
            if row["win_rate"] >= min_win_rate
            and row["realized_quote"] >= min_realized}


def main() -> None:
    ap = argparse.ArgumentParser(description="Smart-wallet PnL tracking")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="ingest curve trades from recent launches")
    sc.add_argument("--hours", type=float, default=24)
    sc.add_argument("--limit", type=int, default=150)
    tp = sub.add_parser("top", help="wallet leaderboard from accumulated data")
    tp.add_argument("--min-tokens", type=int, default=SMART_MIN_TOKENS)
    args = ap.parse_args()

    if args.cmd == "scan":
        scan(args.hours, args.limit)
        return
    rows = leaderboard(args.min_tokens)
    if not rows:
        print("No wallet data yet — run: python -m src.onchain.wallets scan")
        return
    smart = smart_set()
    print(f"{'wallet':44} {'tokens':>6} {'wins':>5} {'winrate':>8} "
          f"{'realized(q)':>12}")
    for r in rows[:40]:
        tag = "  << SMART" if r["wallet"] in smart else ""
        print(f"{r['wallet']:44} {r['tokens']:>6} {r['wins']:>5} "
              f"{r['win_rate']:>7.0%} {r['realized_quote']:>12.3f}{tag}")
    print(f"\n{len(smart)} wallet(s) meet the smart-money bar "
          f"(≥{SMART_MIN_TOKENS} tokens, ≥{SMART_MIN_WIN_RATE:.0%} win rate, "
          f"≥{SMART_MIN_REALIZED_QUOTE} quote realized). Caveat: realized PnL "
          "only — a consistently-profitable wallet may also be an insider whose "
          "profits come from dumping on copiers; cross-check against bundle flags.")


if __name__ == "__main__":
    main()
