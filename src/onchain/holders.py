"""Holder distribution reconstructed from Transfer logs.

Designed for young tokens (the use case): replaying every Transfer since deploy
is cheap when the token is days old. For mature tokens, cap the scan or use an
indexer instead.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .early_buyers import TRANSFER_TOPIC
from .rpc import EvmRpc

ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"


@dataclass
class HolderStats:
    holder_count: int = 0
    top10_pct: float = 0.0
    top20_pct: float = 0.0
    burned_pct: float = 0.0
    top_holders: list[tuple[str, int, float]] = field(default_factory=list)  # (addr, bal, pct)
    circulating: int = 0
    scanned_from_block: int = 0


def holder_stats(rpc: EvmRpc, token: str, deploy_block: int,
                 exclude: set[str] | None = None, top_n: int = 20) -> HolderStats:
    """exclude: addresses to leave out of concentration math (pair, router, locker)."""
    exclude = {a.lower() for a in (exclude or set())}
    latest = rpc.latest_block()
    logs = rpc.get_logs(deploy_block, latest, address=token, topics=[TRANSFER_TOPIC])

    balances: dict[str, int] = defaultdict(int)
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        src = "0x" + topics[1][-40:]
        dst = "0x" + topics[2][-40:]
        amount = int(log["data"], 16) if log.get("data") not in (None, "0x") else 0
        balances[src] -= amount
        balances[dst] += amount

    burned = balances.get(ZERO, 0) * -1  # net minted
    burned_held = balances.get(DEAD, 0) + max(balances.get(ZERO, 0), 0)
    total_minted = max(-balances.get(ZERO, 0), 0)

    held = {a: b for a, b in balances.items()
            if b > 0 and a not in (ZERO, DEAD) and a not in exclude}
    circulating = sum(held.values())

    stats = HolderStats(
        holder_count=len(held),
        circulating=circulating,
        scanned_from_block=deploy_block,
    )
    if total_minted:
        stats.burned_pct = 100.0 * burned_held / total_minted
    if circulating:
        ranked = sorted(held.items(), key=lambda kv: -kv[1])
        stats.top_holders = [(a, b, 100.0 * b / circulating) for a, b in ranked[:top_n]]
        stats.top10_pct = sum(p for _, _, p in stats.top_holders[:10])
        stats.top20_pct = sum(p for _, _, p in stats.top_holders[:20])
    _ = burned  # net-mint figure kept for future supply audits
    return stats
