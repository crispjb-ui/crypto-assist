"""Cluster analysis on early buyers: common funding sources, wallet freshness,
and offload detection (do the snipers still hold what they bought?).
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config, explorer
from .early_buyers import TRANSFER_TOPIC, LaunchWindow
from .erc20 import SEL_BALANCE_OF
from .rpc import EvmRpc, RpcError

# Funding traces are 1-2 explorer HTTP calls per wallet; run them in parallel
# and only for the earliest buyers (where coordination shows). This is the
# difference between a ~90s and a ~10s quick scan.
FUNDING_TRACE_MAX = 12
FUNDING_TRACE_WORKERS = 8


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
    funder_via_contract: bool = False    # funding ran through a disperser/airdrop
    recycle_txs: int = 0                 # same-tx sell+rebuy-to-new-wallet count
    recycle_new_recipients: int = 0      # distinct fresh recipients of recycles


def _origin_of_funding(rpc: EvmRpc, ev: dict) -> str | None:
    """Resolve the human funder behind a funding event. For a direct transfer
    that is the sender; for a contract-mediated one (disperser/airdrop) it is
    the parent transaction's origin, not the contract."""
    if not ev.get("internal"):
        return ev.get("from")
    txh = ev.get("hash")
    if not txh:
        return ev.get("from")
    try:
        tx = rpc.call("eth_getTransactionByHash", [txh])
    except RpcError:
        return ev.get("from")
    return (tx.get("from") or "").lower() if isinstance(tx, dict) else ev.get("from")


def detect_recycling(transfer_logs: list[dict], pair: str) -> tuple[int, int]:
    """Same-transaction sell + rebuy-to-a-different-wallet: the choreography
    that broadens apparent distribution without new net capital. Pure function
    over token Transfer logs; counts such txs and the distinct fresh recipients.
    """
    pair_t = pair.lower()
    by_tx: dict[str, dict[str, list]] = defaultdict(lambda: {"sell": [], "buy": []})
    for log in transfer_logs:
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        src = "0x" + topics[1][-40:]
        dst = "0x" + topics[2][-40:]
        txh = log.get("transactionHash")
        if src == pair_t:
            by_tx[txh]["buy"].append(dst)      # pair -> wallet (a buy)
        elif dst == pair_t:
            by_tx[txh]["sell"].append(src)     # wallet -> pair (a sell)
    recycle_txs = 0
    recipients: set[str] = set()
    for sides in by_tx.values():
        if sides["sell"] and sides["buy"]:
            sellers = set(sides["sell"])
            fresh = [b for b in sides["buy"] if b not in sellers and b != pair_t]
            if fresh:
                recycle_txs += 1
                recipients.update(fresh)
    return recycle_txs, len(recipients)


def analyze_clusters(rpc: EvmRpc, token: str, launch: LaunchWindow,
                     fresh_nonce_max: int = 3,
                     offload_retention_pct: float = 10.0) -> ClusterReport:
    report = ClusterReport()
    launch_block = launch.creation_block

    # Batched historical nonces and current balances — one round-trip each
    # instead of a call per buyer.
    nonce_calls = [("eth_getTransactionCount", [b.wallet, hex(launch_block)])
                   for b in launch.buyers]
    balance_calls = [
        ("eth_call", [{"to": token, "data": SEL_BALANCE_OF
                       + b.wallet.lower().replace("0x", "").rjust(64, "0")},
                      "latest"])
        for b in launch.buyers]
    nonces = rpc.batch(nonce_calls)
    balances = rpc.batch(balance_calls)

    # Parallel funding traces for the earliest buyers (explorer HTTP-bound).
    traced = launch.buyers[:FUNDING_TRACE_MAX]
    with ThreadPoolExecutor(max_workers=FUNDING_TRACE_WORKERS) as pool:
        events = list(pool.map(
            lambda b: explorer.funding_event(b.wallet), traced))
    funding_by_wallet = {b.wallet: ev for b, ev in zip(traced, events)}

    # Resolve the human origin behind disperser-mediated funding in ONE batched
    # round trip (was one sequential tx fetch per internal event).
    internal_hashes = sorted({ev["hash"] for ev in events
                              if ev and ev.get("internal") and ev.get("hash")})
    origin_by_hash: dict[str, str] = {}
    if internal_hashes:
        txs = rpc.batch([("eth_getTransactionByHash", [h])
                         for h in internal_hashes])
        for h, tx in zip(internal_hashes, txs):
            if isinstance(tx, dict) and tx.get("from"):
                origin_by_hash[h] = tx["from"].lower()

    by_funder: dict[str, list[str]] = defaultdict(list)
    for buyer, nonce_hex, bal_hex in zip(launch.buyers, nonces, balances):
        p = BuyerProfile(
            wallet=buyer.wallet,
            bought=buyer.bought,
            blocks_after_creation=buyer.blocks_after_creation,
        )
        if isinstance(nonce_hex, str):
            p.nonce_at_launch = int(nonce_hex, 16)
            if p.nonce_at_launch <= fresh_nonce_max:
                report.fresh_wallet_count += 1

        ev = funding_by_wallet.get(buyer.wallet)
        if ev is not None:
            report.funding_traced = True
            if ev.get("internal"):
                report.funder_via_contract = True
                funder = origin_by_hash.get(ev.get("hash") or "", ev.get("from"))
            else:
                funder = ev.get("from")
            p.funder = funder
            if funder:
                by_funder[funder].append(buyer.wallet)

        if isinstance(bal_hex, str) and bal_hex not in ("", "0x"):
            p.still_holds = int(bal_hex, 16)
        p.retention_pct = (100.0 * p.still_holds / buyer.bought) if buyer.bought else 0.0
        if buyer.bought and p.retention_pct < offload_retention_pct:
            report.offloaded_count += 1

        report.profiles.append(p)

    report.funding_clusters = {f: ws for f, ws in by_funder.items() if len(ws) >= 2}

    # Redistribution recycling: scan the token's Transfer logs across the
    # post-launch window (minutes, so a wider window than the buyer snapshot).
    try:
        window = rpc.blocks_for_seconds(900, floor=200, cap=8_000)
        logs = rpc.get_logs(launch_block, launch_block + window,
                            address=token, topics=[TRANSFER_TOPIC])
        report.recycle_txs, report.recycle_new_recipients = \
            detect_recycling(logs, launch.pair)
    except RpcError:
        pass
    return report
