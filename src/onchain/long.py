"""Long (long.xyz) launchpad watcher for Robinhood Chain.

Long launches pair new tokens against tokenized stocks (NVDA, TSLA, ...) and
create a Uniswap v4 pool atomically in the launch transaction. There is no
published contracts repo; the factory address, launch selector, and event
topic below were derived empirically from the launch transaction of the
flagship Long token (Artificial Inu, AI/NVDA) via derive_factory.py, and the
Uniswap v4 events were identified by keccak-matching their observed topic0
hashes. Layout of the factory's own launch event is unknown, so decoding
works from the transaction receipt instead: the minted token, the v4
Initialize event (poolId + the two currencies), and the tx sender.

    python -m src.onchain.long --hours 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict

from .erc20 import _decode_string, SEL_SYMBOL, balance_of
from .pons import block_near_time
from .rpc import EvmRpc

# Derived from AI/NVDA launch tx
# 0x7632524cd4cec7cabc574b58c54095a2ca33a2a1b037b1486e8b88b79bd3bf1b.
LONG_FACTORY = "0x22e99278308b393ea1260859b181ad7e78f5eeed"
SEL_LONG_LAUNCH = "0x882db707"          # entrypoint function (ABI unknown)
# The factory's own launch event (name unknown; used only as a launch marker).
TOPIC_LONG_LAUNCH = "0xadc6f1f726f7c710f77ec06adc75f3bb964e5be19581b072c67f7b9b4039267b"

# Uniswap v4 PoolManager on Robinhood Chain (observed emitting Initialize in
# the same launch tx; singleton that hosts all v4 pools).
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
# Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)
TOPIC_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
# Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)
TOPIC_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
# Transfer(address,address,uint256)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC = "0x" + "0" * 64


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


@dataclass
class LongLaunch:
    token: str
    symbol: str
    pool_id: str
    paired_token: str            # the tokenized stock side
    paired_symbol: str
    deployer: str
    block: int
    tx_hash: str
    early_buyers: int = 0
    early_buys: int = 0
    buys_in_launch_block: int = 0
    early_bought: int = 0        # raw token units leaving the pool in the window
    offloaded_top: int = 0       # of the top-10 early buyers, how many hold <10%
    buyer_wallets: list[str] = field(default_factory=list)


def parse_launch_receipt(receipt: dict, tx_from: str) -> LongLaunch | None:
    """Extract token, pool, and stock pair from a Long launch tx receipt."""
    token = pool_id = currency0 = currency1 = None
    minted: list[str] = []
    owned: set[str] = set()
    for log in receipt.get("logs") or []:
        addr = log["address"].lower()
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        if t0 == TRANSFER_TOPIC and len(topics) >= 3 and topics[1] == ZERO_TOPIC:
            minted.append(addr)
        elif t0.startswith("0x8be0079c"):  # OwnershipTransferred
            owned.add(addr)
        elif addr == POOL_MANAGER and t0 == TOPIC_V4_INITIALIZE and len(topics) >= 4:
            pool_id = topics[1]
            currency0 = _addr(topics[2])
            currency1 = _addr(topics[3])
    for cand in minted:
        if cand in owned:
            token = cand
            break
    token = token or (minted[0] if minted else None)
    if not token or not pool_id:
        return None
    paired = currency1 if currency0 == token else currency0
    return LongLaunch(
        token=token, symbol="?", pool_id=pool_id,
        paired_token=paired or "?", paired_symbol="?",
        deployer=tx_from.lower(),
        block=int(receipt.get("blockNumber", "0x0"), 16),
        tx_hash=receipt.get("transactionHash", "?"),
    )


def early_activity(rpc: EvmRpc, launch: LongLaunch, window_blocks: int = 2_000,
                   offload_retention_pct: float = 10.0) -> None:
    """Buys = token Transfers out of the PoolManager right after launch."""
    logs = rpc.get_logs(
        launch.block, launch.block + window_blocks, address=launch.token,
        topics=[TRANSFER_TOPIC, "0x" + POOL_MANAGER[2:].rjust(64, "0")],
    )
    tx_hashes = sorted({log["transactionHash"] for log in logs})
    txs = rpc.batch([("eth_getTransactionByHash", [h]) for h in tx_hashes])
    sender_of = {h: (tx.get("from") or "").lower()
                 for h, tx in zip(tx_hashes, txs) if isinstance(tx, dict)}

    bought: dict[str, int] = {}
    for log in logs:
        wallet = sender_of.get(log["transactionHash"]) or _addr(log["topics"][2])
        amount = int(log["data"], 16) if log.get("data") not in (None, "0x") else 0
        bought[wallet] = bought.get(wallet, 0) + amount
        launch.early_buys += 1
        launch.early_bought += amount
        if int(log["blockNumber"], 16) == launch.block:
            launch.buys_in_launch_block += 1

    launch.early_buyers = len(bought)
    top = sorted(bought.items(), key=lambda kv: -kv[1])[:10]
    launch.buyer_wallets = [w for w, _ in top]
    for wallet, amount in top:
        if not amount:
            continue
        held = balance_of(rpc, launch.token, wallet)
        if 100.0 * held / amount < offload_retention_pct:
            launch.offloaded_top += 1


def recent_launches(rpc: EvmRpc, from_block: int, to_block: int,
                    deep: bool = True, limit: int = 50,
                    window_blocks: int = 2_000) -> list[LongLaunch]:
    marker_logs = rpc.get_logs(from_block, to_block, address=LONG_FACTORY,
                               topics=[TOPIC_LONG_LAUNCH])
    tx_hashes = sorted({log["transactionHash"] for log in marker_logs})
    print(f"found {len(tx_hashes)} Long launch tx(s); decoding receipts...",
          file=sys.stderr)
    receipts = rpc.batch([("eth_getTransactionReceipt", [h]) for h in tx_hashes])
    txs = rpc.batch([("eth_getTransactionByHash", [h]) for h in tx_hashes])

    launches: list[LongLaunch] = []
    for rcpt, tx in zip(receipts, txs):
        if not isinstance(rcpt, dict) or not isinstance(tx, dict):
            continue
        launch = parse_launch_receipt(rcpt, tx.get("from") or "")
        if launch:
            launches.append(launch)
    launches.sort(key=lambda l: -l.block)
    launches = launches[:limit]

    symbols = rpc.batch(
        [("eth_call", [{"to": l.token, "data": SEL_SYMBOL}, "latest"]) for l in launches]
        + [("eth_call", [{"to": l.paired_token, "data": SEL_SYMBOL}, "latest"])
           for l in launches])
    n = len(launches)
    for i, launch in enumerate(launches):
        if isinstance(symbols[i], str):
            launch.symbol = _decode_string(symbols[i])
        if isinstance(symbols[n + i], str):
            launch.paired_symbol = _decode_string(symbols[n + i])

    if deep:
        for i, launch in enumerate(launches, 1):
            early_activity(rpc, launch, window_blocks=window_blocks)
            if i % 10 == 0 or i == len(launches):
                print(f"  analyzed {i}/{len(launches)}", file=sys.stderr)
    return launches


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch Long (long.xyz) launches")
    ap.add_argument("--hours", type=float, default=6, help="lookback window")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fast", action="store_true", help="skip early-buy analysis")
    ap.add_argument("--window-mins", type=float, default=5,
                    help="early-buy window after launch, in minutes")
    args = ap.parse_args()

    rpc = EvmRpc()
    to_block = rpc.latest_block()
    print("locating start block...", file=sys.stderr)
    from_block = block_near_time(rpc, int(time.time() - args.hours * 3600))
    print(f"scanning blocks {from_block}-{to_block} for Long launches...",
          file=sys.stderr)
    # Robinhood Chain blocks run ~100-150ms; convert minutes to measured blocks.
    window = rpc.blocks_for_seconds(args.window_mins * 60, floor=200, cap=50_000)
    launches = recent_launches(rpc, from_block, to_block,
                               deep=not args.fast, limit=args.limit,
                               window_blocks=window)

    if args.json:
        print(json.dumps([asdict(l) for l in launches], indent=2))
        return

    print(f"\n{len(launches)} Long launches in blocks {from_block}–{to_block} "
          f"(~{args.hours}h)\n")
    for l in launches:
        parts = [l.symbol.ljust(10)[:10], f"vs {l.paired_symbol}",
                 l.token, f"blk {l.block}"]
        if l.early_buys:
            parts.append(f"{l.early_buys} early buys / {l.early_buyers} wallets")
        if l.buys_in_launch_block:
            parts.append(f"{l.buys_in_launch_block} in launch block")
        if l.offloaded_top:
            parts.append(f"{l.offloaded_top}/10 top buyers offloaded")
        print("  ".join(parts))
    print("\nDeep-check: python -m src.onchain.report <token> "
          f"--pair {POOL_MANAGER} --creation-block <blk>")


if __name__ == "__main__":
    main()
