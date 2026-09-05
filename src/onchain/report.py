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


class NoPairError(RuntimeError):
    pass


def _curve_snipe_activity(rpc, token: str, pair: str, creation_block: int):
    """Execution-truth bundle check for Pons-curve pairs: classify launch-window
    CurveBuy events into tax-free (insider/exempt) vs taxed (organic) buys.
    Returns a PonsLaunch carrying the counts, or None when the pair emits no
    curve events (any non-Pons pool)."""
    from . import explorer
    from .pons import PonsLaunch, TOPIC_CURVE_BUY, snipe_window_activity
    try:
        window = rpc.blocks_for_seconds(30, floor=100, cap=2_000)
        probe = rpc.get_logs(creation_block, creation_block + window,
                             address=pair, topics=[TOPIC_CURVE_BUY])
        if not probe:
            return None
        deployer = ""
        txh = explorer.contract_creation_tx(pair)
        if txh:
            tx = rpc.call("eth_getTransactionByHash", [txh])
            if isinstance(tx, dict):
                deployer = (tx.get("from") or "").lower()
        pl = PonsLaunch(version=2, token=token.lower(), curve_or_pool=pair,
                        deployer=deployer, block=creation_block, tx_hash="")
        snipe_window_activity(rpc, pl, window_blocks=window)
        return pl
    except Exception as exc:
        print(f"note: curve snipe check skipped ({exc})", file=sys.stderr)
        return None


def collect(token: str, pair: str | None = None, bundle_only: bool = False,
            creation_block: int | None = None, log: bool = True,
            rpc: EvmRpc | None = None) -> dict:
    """Run the full diligence pipeline and return the payload as plain data.
    Raises NoPairError when no pool can be located. Logs to the outcome
    ledger unless log=False."""
    rpc = rpc or EvmRpc()
    market = None
    if not pair and config.DEXSCREENER_CHAIN_ID:
        try:
            best = dexscreener.best_pair_for_token(token, config.DEXSCREENER_CHAIN_ID)
            if best:
                pair = best.get("pairAddress")
                market = best
        except Exception as exc:
            print(f"note: DexScreener lookup failed ({exc}); pass --pair",
                  file=sys.stderr)
    if not pair:
        raise NoPairError(
            "No pair found — DexScreener does not index this token yet. "
            "Provide the pool/curve address explicitly.")

    import time as _time
    timings: dict[str, float] = {}

    def _mark(name: str, t0: float) -> None:
        timings[name] = round(_time.monotonic() - t0, 2)

    # Derive the creation block from DexScreener's pairCreatedAt whenever we
    # have it — for a NEW pair the explorer usually hasn't indexed the
    # creation tx yet, and the fallback deploy-block binary search (~25
    # sequential calls) was the main cost of scanning fresh launches.
    if creation_block is None and (market or {}).get("pairCreatedAt"):
        from .pons import block_near_time
        t0 = _time.monotonic()
        creation_block = block_near_time(
            rpc, int(market["pairCreatedAt"]) // 1000)
        _mark("creation_from_timestamp", t0)

    if len(pair) > 42:
        # A 32-byte id, not an address: a Uniswap v4 poolId (DexScreener
        # reports these for v4 markets). The liquidity lives in the shared
        # PoolManager; the singleton's deploy block is meaningless here.
        from .long import POOL_MANAGER
        if creation_block is None:
            raise NoPairError(
                "This token trades in a Uniswap v4 pool (poolId, no pool "
                "address). Provide the launch/creation block — the launch "
                "feeds print it per token.")
        pair = POOL_MANAGER

    t0 = _time.monotonic()
    token_info = inspect_token(rpc, token, include_source=False)
    _mark("metadata", t0)
    t0 = _time.monotonic()
    launch = analyze_launch(rpc, token, pair, creation_block=creation_block)
    _mark("launch_window", t0)

    # The four remaining phases are independent given `launch` — run them
    # concurrently (each on its own RPC client; a shared requests.Session is
    # not guaranteed thread-safe). This is most of the scan's wall time.
    from concurrent.futures import ThreadPoolExecutor
    from .erc20 import apply_source_checks
    from .explorer import get_contract_source

    def _clone() -> EvmRpc:
        c = EvmRpc(rpc.url)
        c._block_rate = rpc._block_rate
        return c

    def _holders_phase():
        from .early_buyers import resolve_creation_block
        hrpc = _clone()
        try:
            token_deploy = resolve_creation_block(
                hrpc, token, hi=launch.window_end_block + 1)
        except RpcError:
            token_deploy = launch.creation_block
        return holder_stats(hrpc, token, token_deploy, exclude={pair})

    def _timed(name: str, fn, *a):
        t0 = _time.monotonic()
        try:
            return fn(*a)
        finally:
            _mark(name, t0)

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_source = ex.submit(_timed, "source", get_contract_source, token)
        f_curve = ex.submit(_timed, "curve_bundle", _curve_snipe_activity,
                            _clone(), token, pair, launch.creation_block)
        f_clusters = ex.submit(_timed, "clusters", analyze_clusters,
                               _clone(), token, launch)
        f_holders = (ex.submit(_timed, "holders", _holders_phase)
                     if not bundle_only else None)
        clusters = f_clusters.result()
        curve = f_curve.result()
        holders = f_holders.result() if f_holders is not None else None
        try:
            apply_source_checks(token_info, f_source.result())
        except Exception as exc:
            print(f"note: source check skipped ({exc})", file=sys.stderr)
    print("scan timings: " + ", ".join(f"{k}={v}s" for k, v in
                                       sorted(timings.items(),
                                              key=lambda kv: -kv[1])),
          file=sys.stderr)

    verdict = evaluate(token_info, launch, clusters, holders)
    if curve is not None:
        if curve.exempt_buys >= 3:
            verdict.add(3, f"{curve.exempt_buys} tax-free snipe buys "
                           f"({curve.exempt_buy_quote / 1e18:.2f} quote) in the "
                           "launch window — execution-proven bundle",
                        "curve-bundle-execution")
        elif curve.exempt_buys >= 1:
            verdict.add(1, f"{curve.exempt_buys} tax-free snipe buy(s) in the "
                           "launch window", "curve-bundle-execution-low")
    payload = {
        "token": asdict(token_info),
        "pair": pair,
        "market": market,
        "launch": asdict(launch),
        "clusters": asdict(clusters),
        "holders": asdict(holders) if holders else None,
        "curve": ({"exempt_buys": curve.exempt_buys,
                   "taxed_buys": curve.taxed_buys,
                   "exempt_buy_quote": curve.exempt_buy_quote,
                   "exempt_buyer_wallets": curve.exempt_buyer_wallets}
                  if curve is not None else None),
        "verdict": asdict(verdict),
        "verdict_line": verdict_line(verdict),
        "timings": timings,
    }
    if log:
        try:
            store.log_run(
                token=token, pair=pair,
                kind="bundle-only" if bundle_only else "report",
                score=verdict.score,
                flags=[asdict(f) for f in verdict.flags],
                notes=verdict.notes, market=market,
                data={k: payload[k] for k in ("token", "launch", "clusters",
                                              "holders")},
            )
        except Exception as exc:
            print(f"note: run ledger write failed ({exc})", file=sys.stderr)
    return payload


def run(token: str, pair: str | None, bundle_only: bool, as_json: bool,
        log: bool = True, creation_block: int | None = None) -> int:
    rpc = EvmRpc()
    if config.EVM_CHAIN_ID:
        actual = rpc.chain_id()
        if str(actual) != str(config.EVM_CHAIN_ID):
            print(f"WARNING: RPC serves chain id {actual}, .env expects "
                  f"{config.EVM_CHAIN_ID}", file=sys.stderr)

    try:
        payload = collect(token, pair, bundle_only, creation_block, log, rpc)
    except NoPairError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    # re-hydrate the pieces the text renderer uses
    from .clusters import ClusterReport
    from .early_buyers import EarlyBuyer, LaunchWindow
    from .erc20 import TokenInfo
    from .holders import HolderStats
    from .score import Flag, Verdict
    token_info = TokenInfo(**payload["token"])
    launch = LaunchWindow(**{
        **payload["launch"],
        "buyers": [EarlyBuyer(**b) for b in payload["launch"]["buyers"]]})
    clusters = ClusterReport(**{**payload["clusters"], "profiles": []})
    holders = HolderStats(**payload["holders"]) if payload["holders"] else None
    if holders:
        holders.top_holders = [tuple(t) for t in holders.top_holders]
    verdict = Verdict(score=payload["verdict"]["score"],
                      flags=[Flag(**f) for f in payload["verdict"]["flags"]],
                      notes=payload["verdict"]["notes"])
    market, pair = payload["market"], payload["pair"]

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
