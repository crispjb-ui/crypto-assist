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

from . import long as long_mod
from . import pons, store
from .erc20 import SEL_DECIMALS, _decode_uint
from .long import POOL_MANAGER, TRANSFER_TOPIC
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


def ingest_long_trades(rpc: EvmRpc, token: str, paired: str, paired_symbol: str,
                       launch_block: int, window_blocks: int) -> int:
    """Aggregate one Long launch's trades into the wallet ledger.

    Long trades settle inside the v4 PoolManager singleton, so PnL is
    reconstructed from Transfer flows: the token leg identifies the trade
    transactions, the paired (stock-token) leg carries the quote amounts, and
    each amount is attributed to the transaction sender (router hops make the
    raw transfer counterparty unreliable). Realized PnL is in units of the
    PAIRED token (NVDA, AI, ...), recorded under that quote symbol.
    """
    pm_topic = "0x" + POOL_MANAGER[2:].rjust(64, "0")
    lo, hi = launch_block, launch_block + window_blocks
    tok_buys = rpc.get_logs(lo, hi, address=token, topics=[TRANSFER_TOPIC, pm_topic])
    tok_sells = rpc.get_logs(lo, hi, address=token,
                             topics=[TRANSFER_TOPIC, None, pm_topic])
    trade_txs = {l["transactionHash"] for l in tok_buys + tok_sells}
    if not trade_txs:
        return 0

    # paired-token flows through the PM in those same transactions
    pr_out = [l for l in rpc.get_logs(lo, hi, address=paired,
                                      topics=[TRANSFER_TOPIC, pm_topic])
              if l["transactionHash"] in trade_txs]
    pr_in = [l for l in rpc.get_logs(lo, hi, address=paired,
                                     topics=[TRANSFER_TOPIC, None, pm_topic])
             if l["transactionHash"] in trade_txs]

    tx_hashes = sorted(trade_txs)
    txs = rpc.batch([("eth_getTransactionByHash", [h]) for h in tx_hashes])
    sender_of = {h: (tx.get("from") or "").lower()
                 for h, tx in zip(tx_hashes, txs) if isinstance(tx, dict)}
    dec = _decode_uint(rpc.eth_call(paired, SEL_DECIMALS)) or 18

    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    for log in pr_in:      # quote INTO the PM = wallet spent it (bought token)
        wallet = sender_of.get(log["transactionHash"])
        if wallet:
            agg[wallet][0] += int(log["data"], 16) / 10 ** dec
            agg[wallet][2] += 1
    for log in pr_out:     # quote OUT of the PM = wallet received it (sold)
        wallet = sender_of.get(log["transactionHash"])
        if wallet:
            agg[wallet][1] += int(log["data"], 16) / 10 ** dec
            agg[wallet][2] += 1
    for wallet, (spent, received, trades) in agg.items():
        store.upsert_wallet_trade(wallet, token, spent, received, trades,
                                  quote_symbol=paired_symbol or "?")
    return len(pr_in) + len(pr_out)


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
        print(f"  pons [{i}/{len(launches)}] {l.token} — {n} trade events",
              file=sys.stderr)

    long_launches = long_mod.recent_launches(rpc, from_block, to_block,
                                             deep=False, limit=limit)
    for i, l in enumerate(long_launches, 1):
        if store.wallet_scan_done(l.token):
            continue
        n = ingest_long_trades(rpc, l.token, l.paired_token, l.paired_symbol,
                               l.block, window)
        store.mark_wallet_scan(l.token)
        new += 1
        print(f"  long [{i}/{len(long_launches)}] {l.token} — {n} quote legs",
              file=sys.stderr)
    print(f"ingested {new} new launch(es) into the wallet ledger")


def leaderboard(min_tokens: int = SMART_MIN_TOKENS) -> list[dict]:
    """Win rate is unitless and aggregates across venues; realized PnL is only
    meaningful per quote currency (ETH for Pons, stock tokens for Long) and is
    therefore reported per quote, never summed across them."""
    from . import config
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT wallet, quote_symbol, COUNT(*) AS tokens, "
            "SUM(received - spent) AS realized, "
            "SUM(CASE WHEN received > spent THEN 1 ELSE 0 END) AS wins, "
            "SUM(trades) AS trades "
            "FROM wallet_trades WHERE (chain = ? OR chain = '') "
            "GROUP BY wallet, quote_symbol",
            (config.CHAIN_KEY,),
        ).fetchall()
    per_wallet: dict[str, dict] = {}
    for (w, q, t, r, wi, tr) in rows:
        e = per_wallet.setdefault(w, {"wallet": w, "tokens": 0, "wins": 0,
                                      "trades": 0, "realized_by_quote": {}})
        e["tokens"] += t
        e["wins"] += wi
        e["trades"] += tr
        e["realized_by_quote"][q] = (e["realized_by_quote"].get(q, 0.0)
                                     + (r or 0.0))
    out = [e for e in per_wallet.values() if e["tokens"] >= min_tokens]
    for e in out:
        e["win_rate"] = e["wins"] / e["tokens"] if e["tokens"] else 0.0
    out.sort(key=lambda e: (-e["win_rate"],
                            -max(e["realized_by_quote"].values(), default=0.0)))
    return out


def smart_set(min_tokens: int = SMART_MIN_TOKENS,
              min_win_rate: float = SMART_MIN_WIN_RATE,
              min_realized: float = SMART_MIN_REALIZED_QUOTE) -> set[str]:
    """Smart = enough sample, real win rate, no net-losing book in any quote,
    and a meaningful realized total in at least one quote. Quote units are not
    comparable across currencies (0.5 ETH != 0.5 NVDA) — the threshold is an
    order-of-magnitude bar, and the calibration loop is the place to tune it."""
    smart = set()
    for e in leaderboard(min_tokens):
        by_q = e["realized_by_quote"]
        if (e["win_rate"] >= min_win_rate
                and all(v >= 0 for v in by_q.values())
                and any(v >= min_realized for v in by_q.values())):
            smart.add(e["wallet"])
    return smart


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
    print(f"{'wallet':44} {'tokens':>6} {'wins':>5} {'winrate':>8}  realized")
    for r in rows[:40]:
        tag = "  << SMART" if r["wallet"] in smart else ""
        realized = ", ".join(f"{v:+.3f} {q}" for q, v in
                             sorted(r["realized_by_quote"].items(),
                                    key=lambda kv: -kv[1]))
        print(f"{r['wallet']:44} {r['tokens']:>6} {r['wins']:>5} "
              f"{r['win_rate']:>7.0%}  {realized}{tag}")
    print(f"\n{len(smart)} wallet(s) meet the smart-money bar "
          f"(≥{SMART_MIN_TOKENS} tokens, ≥{SMART_MIN_WIN_RATE:.0%} win rate, "
          f"≥{SMART_MIN_REALIZED_QUOTE} quote realized). Caveat: realized PnL "
          "only — a consistently-profitable wallet may also be an insider whose "
          "profits come from dumping on copiers; cross-check against bundle flags.")


if __name__ == "__main__":
    main()
