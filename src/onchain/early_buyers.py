"""Extract early buyers: wallets that received tokens from the pair in the first
N blocks after pair creation. DEX-agnostic — works off ERC-20 Transfer logs, so it
covers any AMM style (V2/V3/custom) without knowing the Swap event layout.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .rpc import EvmRpc

# keccak-256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _topic_addr(address: str) -> str:
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


@dataclass
class EarlyBuyer:
    wallet: str
    bought: int = 0                      # raw token units received from pair
    first_block: int = 0
    blocks_after_creation: int = 0
    tx_hashes: list[str] = field(default_factory=list)


@dataclass
class LaunchWindow:
    pair: str
    creation_block: int
    creation_time: int
    window_end_block: int
    buyers: list[EarlyBuyer] = field(default_factory=list)
    total_bought_in_window: int = 0
    buys_in_creation_block: int = 0


def analyze_launch(rpc: EvmRpc, token: str, pair: str,
                   window_blocks: int | None = None,
                   creation_block: int | None = None) -> LaunchWindow:
    """creation_block overrides the pair-deploy binary search — required for
    singleton pools (Uniswap v4 PoolManager), where the shared contract's
    deploy block says nothing about this token's launch."""
    window_blocks = window_blocks or config.EARLY_WINDOW_BLOCKS
    if creation_block is None:
        creation_block = rpc.find_deploy_block(pair)
    end_block = creation_block + window_blocks

    logs = rpc.get_logs(
        creation_block, end_block, address=token,
        topics=[TRANSFER_TOPIC, _topic_addr(pair)],  # topic1 = from == pair
    )

    result = LaunchWindow(
        pair=pair.lower(),
        creation_block=creation_block,
        creation_time=rpc.get_block_time(creation_block),
        window_end_block=end_block,
    )

    tx_hashes = sorted({log["transactionHash"] for log in logs})
    txs = rpc.batch([("eth_getTransactionByHash", [h]) for h in tx_hashes])
    sender_of = {
        h: (tx.get("from") or "").lower()
        for h, tx in zip(tx_hashes, txs) if isinstance(tx, dict)
    }

    buyers: dict[str, EarlyBuyer] = {}
    for log in logs:
        block = int(log["blockNumber"], 16)
        amount = int(log["data"], 16) if log.get("data") not in (None, "0x") else 0
        txh = log["transactionHash"]
        # The tx sender is the acting wallet; the Transfer recipient may be a
        # router or a different receiving wallet — fall back to it if needed.
        wallet = sender_of.get(txh) or ("0x" + log["topics"][2][-40:])
        b = buyers.setdefault(wallet, EarlyBuyer(wallet=wallet, first_block=block))
        b.bought += amount
        b.first_block = min(b.first_block, block)
        b.blocks_after_creation = b.first_block - creation_block
        if txh not in b.tx_hashes:
            b.tx_hashes.append(txh)
        result.total_bought_in_window += amount
        if block == creation_block:
            result.buys_in_creation_block += 1

    result.buyers = sorted(buyers.values(), key=lambda b: (b.first_block, -b.bought))
    if len(result.buyers) > config.MAX_EARLY_BUYERS:
        result.buyers = result.buyers[: config.MAX_EARLY_BUYERS]
    return result
