"""Pons launchpad (ponsfamily.com) watcher for Robinhood Chain.

Pons is the dominant token launchpad on Robinhood Chain. Its V2 flow carries a
snipe tax (99% decaying over ~15s at current factory settings) — but the launch
call can declare up to 32 wallets EXEMPT from that tax. That declared list is
the bundle: the operator's sniper cluster, published in the launch transaction
itself. This module extracts it.

Contract addresses and event/function signatures come from the official
contracts repo (github.com/ponsdotdev/ponsfamily); topic0/selector hashes were
computed from those signatures and cross-checked against known Ethereum hashes.

    python -m src.onchain.pons --hours 6
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field, asdict

from .erc20 import _decode_string, SEL_SYMBOL
from .rpc import EvmRpc

# Deployed factories on Robinhood Chain (chain id 4663), per the official repo.
V1_FACTORY = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
V2_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"

# topic0 hashes (keccak-256 of the event signature)
# TokenLaunched(address,address,address,address,address,uint256,uint256,uint256,uint256,uint256)
TOPIC_V1_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
# TokenLaunched(address,address,address,address,uint256,uint256)
TOPIC_V2_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
# PoolGraduated(address,uint256,uint256,uint256)
TOPIC_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"
# CurveBuy(address,address,uint256,uint256,uint256,uint256)
TOPIC_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
# CurveSell(address,address,uint256,uint256,uint256,uint256)
TOPIC_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"

# Function selectors on PonsV2LaunchFactory (TokenParams =
# (string,string,string,string,(string,string,string,string,string),address,uint16,bool,bytes32))
SEL_LAUNCH_PLAIN = "0xa41d5f2b"   # launchToken(TokenParams,uint256,address)
SEL_LAUNCH_EXEMPT = "0x3580febb"  # launchToken(TokenParams,uint256,address,address[])
SEL_LAUNCH_FOR = "0x42236f86"     # launchTokenFor(TokenParams,uint256,address,address,address[])


@dataclass
class PonsLaunch:
    version: int
    token: str
    deployer: str
    curve_or_pool: str            # V2: bonding curve; V1: Uniswap V3 pool
    block: int
    tx_hash: str
    symbol: str = "?"
    declared_exemptions: list[str] = field(default_factory=list)
    exemptions_known: bool = True  # False when the entrypoint wasn't decodable
    graduated: bool = False
    # snipe-window trading (V2 only)
    exempt_buys: int = 0           # tax-free buys in the window = the bundle executing
    taxed_buys: int = 0            # outsiders paying the decaying tax
    exempt_buy_quote: int = 0      # total quote spent by tax-free buyers (wei)


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def exemptions_from_calldata(data: str) -> tuple[list[str], bool]:
    """Decode the declared snipe-tax exemption list from a V2 launch tx.

    Returns (addresses, known). known=False means an unrecognized entrypoint
    (e.g. a new router) — absence of evidence, not evidence of absence.
    """
    sel = data[:10].lower()
    if sel == SEL_LAUNCH_PLAIN:
        return [], True
    if sel == SEL_LAUNCH_EXEMPT:
        arr_slot = 3
    elif sel == SEL_LAUNCH_FOR:
        arr_slot = 4
    else:
        return [], False
    body = data[10:]

    def word(i: int) -> str:
        return body[i * 64 : (i + 1) * 64]

    try:
        offset = int(word(arr_slot), 16) // 32
        count = int(word(offset), 16)
        return ["0x" + word(offset + 1 + i)[-40:] for i in range(min(count, 64))], True
    except (ValueError, IndexError):
        return [], False


def block_near_time(rpc: EvmRpc, target_ts: int) -> int:
    """Approximate block at a unix timestamp via interpolation (Orbit chains
    have variable block times, so sample-and-refine instead of assuming a rate)."""
    hi = rpc.latest_block()
    hi_ts = rpc.get_block_time(hi)
    lo = max(hi - 1, 0)
    guess = hi
    for _ in range(8):
        if hi_ts <= target_ts:
            return hi
        span = max(hi - 200_000, 0)
        span_ts = rpc.get_block_time(span)
        if span_ts >= target_ts or hi_ts == span_ts:
            return span
        rate = (hi - span) / (hi_ts - span_ts)  # blocks per second
        guess = span + int((target_ts - span_ts) * rate)
        guess = max(min(guess, hi), 0)
        guess_ts = rpc.get_block_time(guess)
        if abs(guess_ts - target_ts) < 30:
            return guess
        if guess_ts < target_ts:
            span = guess
        else:
            hi, hi_ts = guess, guess_ts
    return guess


def snipe_window_activity(rpc: EvmRpc, launch: PonsLaunch,
                          window_blocks: int = 80) -> None:
    """Classify CurveBuy events right after launch: tax==0 → declared/bundle
    wallet buying at the untaxed price; tax>0 → outsider paying the snipe tax."""
    logs = rpc.get_logs(launch.block, launch.block + window_blocks,
                        address=launch.curve_or_pool, topics=[TOPIC_CURVE_BUY])
    for log in logs:
        data = log.get("data", "0x")[2:]
        if len(data) < 4 * 64:
            continue
        quote_in = int(data[0:64], 16)
        tax = int(data[192:256], 16)
        if tax == 0:
            buyer = _addr(log["topics"][1])
            if buyer != launch.deployer:
                launch.exempt_buys += 1
                launch.exempt_buy_quote += quote_in
        else:
            launch.taxed_buys += 1


def recent_launches(rpc: EvmRpc, from_block: int, to_block: int,
                    deep: bool = True, limit: int = 200) -> list[PonsLaunch]:
    launches: list[PonsLaunch] = []

    for log in rpc.get_logs(from_block, to_block, address=V2_FACTORY,
                            topics=[TOPIC_V2_LAUNCHED]):
        launches.append(PonsLaunch(
            version=2,
            token=_addr(log["topics"][1]),
            curve_or_pool=_addr(log["topics"][2]),
            deployer=_addr(log["topics"][3]),
            block=int(log["blockNumber"], 16),
            tx_hash=log["transactionHash"],
        ))

    for log in rpc.get_logs(from_block, to_block, address=V1_FACTORY,
                            topics=[TOPIC_V1_LAUNCHED]):
        data = log.get("data", "0x")[2:]
        pool = "0x" + data[64:128][-40:] if len(data) >= 128 else "?"
        launches.append(PonsLaunch(
            version=1,
            token=_addr(log["topics"][1]),
            deployer=_addr(log["topics"][2]),
            curve_or_pool=pool,
            block=int(log["blockNumber"], 16),
            tx_hash=log["transactionHash"],
        ))

    launches.sort(key=lambda l: -l.block)
    launches = launches[:limit]
    if not deep:
        return launches

    graduated_tokens = {
        _addr(log["topics"][1])
        for log in rpc.get_logs(from_block, rpc.latest_block(),
                                address=V2_FACTORY, topics=[TOPIC_GRADUATED])
    }

    txs = rpc.batch([("eth_getTransactionByHash", [l.tx_hash]) for l in launches])
    symbols = rpc.batch([("eth_call", [{"to": l.token, "data": SEL_SYMBOL}, "latest"])
                         for l in launches])
    for launch, tx, sym in zip(launches, txs, symbols):
        if isinstance(sym, str):
            launch.symbol = _decode_string(sym)
        launch.graduated = launch.token in graduated_tokens
        if launch.version == 2 and isinstance(tx, dict):
            launch.declared_exemptions, launch.exemptions_known = \
                exemptions_from_calldata(tx.get("input", ""))
            snipe_window_activity(rpc, launch)
    return launches


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch Pons launches on Robinhood Chain")
    ap.add_argument("--hours", type=float, default=6, help="lookback window")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fast", action="store_true",
                    help="skip per-launch calldata/curve analysis")
    args = ap.parse_args()

    rpc = EvmRpc()
    to_block = rpc.latest_block()
    from_block = block_near_time(rpc, int(time.time() - args.hours * 3600))
    launches = recent_launches(rpc, from_block, to_block,
                               deep=not args.fast, limit=args.limit)

    if args.json:
        print(json.dumps([asdict(l) for l in launches], indent=2))
        return

    print(f"\n{len(launches)} Pons launches in blocks {from_block}–{to_block} "
          f"(~{args.hours}h)\n")
    for l in launches:
        parts = [f"v{l.version}", l.symbol.ljust(10)[:10], l.token,
                 f"blk {l.block}"]
        if l.version == 2:
            if l.declared_exemptions:
                parts.append(f"BUNDLED: {len(l.declared_exemptions)} declared exempt wallets")
            elif not l.exemptions_known:
                parts.append("exemptions: unknown entrypoint")
            if l.exempt_buys:
                parts.append(f"{l.exempt_buys} tax-free snipe buys "
                             f"({l.exempt_buy_quote / 1e18:.3f} quote)")
            if l.taxed_buys:
                parts.append(f"{l.taxed_buys} taxed outside buys")
        if l.graduated:
            parts.append("GRADUATED")
        print("  ".join(parts))
    print("\nDeep-check any token: python -m src.onchain.report <token> "
          "--pair <curve_or_pool>")


if __name__ == "__main__":
    main()
