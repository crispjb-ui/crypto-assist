"""Cluster analysis on early buyers: common funding sources, wallet freshness,
and offload detection (do the snipers still hold what they bought?).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import explorer
from .early_buyers import LaunchWindow
from .erc20 import balance_of
from .rpc import EvmRpc


@dataclass
class BuyerProfile:
    wallet: str
    bought: int
    blocks_after_creation: int
    nonce_at_launch: int | None = None   # low nonce = purpose-made wallet
    funder: str | None = None            # first native-coin sender (needs explorer API)
    still_holds: int = 0
    retention_pct: float = 0.0


@dataclass
class ClusterReport:
    profiles: list[BuyerProfile] = field(default_factory=list)
    funding_clusters: dict[str, list[str]] = field(default_factory=dict)  # funder -> wallets
    fresh_wallet_count: int = 0
    offloaded_count: int = 0
    funding_traced: bool = False


def analyze_clusters(rpc: EvmRpc, token: str, launch: LaunchWindow,
                     fresh_nonce_max: int = 3,
                     offload_retention_pct: float = 10.0) -> ClusterReport:
    report = ClusterReport()
    launch_block = launch.creation_block

    # Nonce at launch (historical state) — best effort, some RPCs prune it.
    nonce_calls = [("eth_getTransactionCount", [b.wallet, hex(launch_block)])
                   for b in launch.buyers]
    nonces = rpc.batch(nonce_calls)

    by_funder: dict[str, list[str]] = defaultdict(list)
    for buyer, nonce_hex in zip(launch.buyers, nonces):
        p = BuyerProfile(
            wallet=buyer.wallet,
            bought=buyer.bought,
            blocks_after_creation=buyer.blocks_after_creation,
        )
        if isinstance(nonce_hex, str):
            p.nonce_at_launch = int(nonce_hex, 16)
            if p.nonce_at_launch <= fresh_nonce_max:
                report.fresh_wallet_count += 1

        funder = explorer.funding_source(buyer.wallet)
        if funder is not None:
            report.funding_traced = True
            p.funder = funder
            by_funder[funder].append(buyer.wallet)

        p.still_holds = balance_of(rpc, token, buyer.wallet)
        p.retention_pct = (100.0 * p.still_holds / buyer.bought) if buyer.bought else 0.0
        if buyer.bought and p.retention_pct < offload_retention_pct:
            report.offloaded_count += 1

        report.profiles.append(p)

    report.funding_clusters = {f: ws for f, ws in by_funder.items() if len(ws) >= 2}
    return report
