"""Full diligence report CLI.

    python -m src.onchain.report 0xTOKEN [--pair 0xPAIR] [--bundle-only] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import config, dexscreener, store
from .clusters import analyze_clusters
from .early_buyers import analyze_launch
from .erc20 import inspect_token
from .holders import holder_stats
from .rpc import EvmRpc, RpcError
from .score import evaluate, verdict_line


def _fmt_amount(raw: int, decimals: int) -> str:
    return f"{raw / 10 ** decimals:,.2f}"


def run(token: str, pair: str | None, bundle_only: bool, as_json: bool,
        log: bool = True, creation_block: int | None = None) -> int:
    rpc = EvmRpc()
    if config.EVM_CHAIN_ID:
        actual = rpc.chain_id()
        if str(actual) != str(config.EVM_CHAIN_ID):
            print(f"WARNING: RPC serves chain id {actual}, .env expects "
                  f"{config.EVM_CHAIN_ID}", file=sys.stderr)

    market = None
    if not pair and config.DEXSCREENER_CHAIN_ID:
        try:
            best = dexscreener.best_pair_for_token(token, config.DEXSCREENER_CHAIN_ID)
            if best:
                pair = best.get("pairAddress")
                market = best
        except Exception as exc:
            print(f"note: DexScreener lookup failed ({exc}); pass --pair", file=sys.stderr)
    if not pair:
        print("No pair found. Pass --pair 0xPAIR (DexScreener does not index this "
              "chain, or the token has no pool).", file=sys.stderr)
        return 2

    token_info = inspect_token(rpc, token)
    launch = analyze_launch(rpc, token, pair, creation_block=creation_block)
    clusters = analyze_clusters(rpc, token, launch)
    holders = None
    if not bundle_only:
        try:
            token_deploy = rpc.find_deploy_block(token, hi=launch.window_end_block + 1)
        except RpcError:
            token_deploy = launch.creation_block
        holders = holder_stats(rpc, token, token_deploy, exclude={pair})

    verdict = evaluate(token_info, launch, clusters, holders)

    if log:
        try:
            store.log_run(
                token=token,
                pair=pair,
                kind="bundle-only" if bundle_only else "report",
                score=verdict.score,
                flags=[asdict(f) for f in verdict.flags],
                notes=verdict.notes,
                market=market,
                data={"token": asdict(token_info), "launch": asdict(launch),
                      "clusters": asdict(clusters),
                      "holders": asdict(holders) if holders else None},
            )
        except Exception as exc:
            print(f"note: run ledger write failed ({exc})", file=sys.stderr)

    if as_json:
        print(json.dumps({
            "token": asdict(token_info),
            "market": market,
            "launch": asdict(launch),
            "clusters": asdict(clusters),
            "holders": asdict(holders) if holders else None,
            "verdict": asdict(verdict),
        }, indent=2, default=str))
        return 0

    d = token_info.decimals
    print(f"\n=== {token_info.name} ({token_info.symbol}) — {token} ===")
    if market:
        liq = (market.get("liquidity") or {}).get("usd")
        print(f"pair {pair}  price ${market.get('priceUsd')}  "
              f"liquidity ${liq:,.0f}" if liq else f"pair {pair}")
    print(f"\nLaunch window (blocks {launch.creation_block}–{launch.window_end_block}):")
    print(f"  buys in creation block: {launch.buys_in_creation_block}")
    print(f"  early buyers analyzed:  {len(launch.buyers)}")
    print(f"  fresh wallets at launch: {clusters.fresh_wallet_count}")
    print(f"  already offloaded:       {clusters.offloaded_count}")
    for funder, wallets in clusters.funding_clusters.items():
        print(f"  funding cluster: {funder} -> {len(wallets)} buyers")
    if holders:
        print(f"\nHolders: {holders.holder_count}  top10 {holders.top10_pct:.1f}%  "
              f"burned {holders.burned_pct:.1f}%")
        for addr, bal, pct in holders.top_holders[:10]:
            print(f"  {addr}  {_fmt_amount(bal, d):>20}  {pct:5.1f}%")

    print(f"\nScore: {verdict.score}")
    for flag in verdict.flags:
        print(f"  [+{flag.points}] {flag.label}")
    for note in verdict.notes:
        print(f"  [note] {note}")
    print(f"\n{verdict_line(verdict)}\n")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="On-chain diligence report")
    ap.add_argument("token")
    ap.add_argument("--pair", help="pool/pair address (required if DexScreener "
                                   "doesn't index the chain)")
    ap.add_argument("--bundle-only", action="store_true",
                    help="skip holder reconstruction (fewer RPC calls)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-log", action="store_true",
                    help="skip writing this run to the outcome ledger")
    ap.add_argument("--creation-block", type=int,
                    help="launch block override — required when --pair is a "
                         "singleton (e.g. the Uniswap v4 PoolManager for "
                         "Long/graduated-Pons tokens)")
    args = ap.parse_args()
    sys.exit(run(args.token, args.pair, args.bundle_only, args.json,
                 log=not args.no_log, creation_block=args.creation_block))


if __name__ == "__main__":
    main()
