"""Red-flag scoring: combine module outputs into a single farm-likelihood score."""
from __future__ import annotations

from dataclasses import dataclass, field

from .clusters import ClusterReport
from .early_buyers import LaunchWindow
from .erc20 import TokenInfo
from .holders import HolderStats


@dataclass
class Flag:
    points: int
    label: str
    category: str = ""   # stable slug for calibration stats, never shown to users


@dataclass
class Verdict:
    score: int = 0
    flags: list[Flag] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, points: int, label: str, category: str) -> None:
        self.flags.append(Flag(points, label, category))
        self.score += points


def evaluate(token: TokenInfo, launch: LaunchWindow | None,
             clusters: ClusterReport | None, holders: HolderStats | None) -> Verdict:
    v = Verdict()

    # --- contract ---
    if token.owner is not None and token.owner_renounced is False:
        v.add(1, f"owner not renounced ({token.owner})", "owner-not-renounced")
    if token.is_proxy:
        v.add(2, "upgradeable proxy — logic can be swapped after you buy", "proxy")
    if token.source_verified is False:
        v.add(2, "contract source not verified", "unverified-source")
    elif token.source_verified is None:
        v.notes.append("no explorer API configured — source verification not checked")
    if token.suspect_source_hits:
        v.add(1, "source contains: " + ", ".join(token.suspect_source_hits)
                 + " (read these functions manually)", "suspect-source")

    # --- launch window ---
    if launch:
        if launch.buys_in_creation_block >= 3:
            v.add(3, f"{launch.buys_in_creation_block} buys in the pair-creation block "
                     "itself — bundled launch", "creation-block-bundle")
        elif launch.buys_in_creation_block >= 1:
            v.add(1, f"{launch.buys_in_creation_block} buy(s) in the creation block", "creation-block-buy")
        same_early = [b for b in launch.buyers if b.blocks_after_creation <= 2]
        if len(same_early) >= 8:
            v.add(2, f"{len(same_early)} distinct wallets bought within 2 blocks of creation", "early-buyer-density")

    # --- clusters ---
    if clusters:
        n = max(len(clusters.profiles), 1)
        if clusters.funding_clusters:
            biggest = max(clusters.funding_clusters.items(), key=lambda kv: len(kv[1]))
            v.add(3, f"{len(biggest[1])} early buyers share funder {biggest[0]} "
                     f"({len(clusters.funding_clusters)} funding cluster(s) total)", "funding-cluster")
        elif not clusters.funding_traced:
            v.notes.append("funding sources not traced (no explorer API, or wallets "
                           "funded cross-chain) — fresh-wallet nonce is the fallback signal")
        if clusters.fresh_wallet_count / n >= 0.5:
            v.add(2, f"{clusters.fresh_wallet_count}/{n} early buyers were fresh wallets "
                     "(nonce ≤ 3 at launch)", "fresh-wallets")
        if clusters.offloaded_count / n >= 0.5:
            v.add(3, f"{clusters.offloaded_count}/{n} early buyers already offloaded "
                     "(<10% of buy retained)", "sniper-offload")

    # --- holders ---
    if holders:
        if holders.top10_pct >= 50:
            v.add(3, f"top 10 holders control {holders.top10_pct:.1f}% of circulating supply", "top10-concentration-high")
        elif holders.top10_pct >= 30:
            v.add(1, f"top 10 holders control {holders.top10_pct:.1f}%", "top10-concentration-mid")
        if holders.holder_count < 100:
            v.add(1, f"only {holders.holder_count} holders", "low-holder-count")

    return v


def verdict_line(v: Verdict) -> str:
    if v.score >= 8:
        return "AVOID — farm pattern"
    if v.score >= 5:
        return "HIGH RISK — treat as a farm until proven otherwise"
    if v.score >= 2:
        return "CAUTION — flags present, read them"
    return "NO MAJOR RED FLAGS — proceed to fundamental research (this is not a buy signal)"
