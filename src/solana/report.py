"""Solana token diligence — the EVM pipeline's detectors, rebuilt on Solana
primitives. Launchpad-agnostic: analysis is driven from the token's
DexScreener pair/pool account (pump.fun curve, PumpSwap/Raydium pool,
stonk.fun stock-paired pool, ...), so any launchpad DexScreener indexes works
without knowing its program in advance. The pool's owner program is measured
from the chain and labeled only on exact match — unknown launchpads (e.g.
stonk.fun until its program id is learned) display the raw program id.

    python -m src.solana.report <mint>

What maps how:
  EVM mint()/owner checks  -> mint & freeze AUTHORITIES read from the mint
                              account (fact, not source grep)
  creation-block bundle    -> buys in the pool's creation slot
  funding clusters         -> earliest inbound System transfer per buyer
  sniper offload           -> current token balance vs amount bought
  holder concentration     -> top-20 token accounts vs supply (Solana RPC has
                              no cheap full holder count — reported honestly)
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from ..onchain import config, dexscreener, store
from ..onchain.score import Verdict, verdict_line
from . import b58
from .mint import parse_mint
from .rpc import SolRpc

CHAIN_KEY = "solana"
DEX_CHAIN = "solana"
LAMPORTS = 1e9
BURN_OWNER = "1nc1nerator11111111111111111111111111111111"

# Venue labels applied ONLY on exact owner-program match; anything else shows
# the measured program id raw. Extend without code via .env:
#   SOLANA_VENUES=stonk.fun:<program-id>;other:<program-id>
KNOWN_VENUE_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun curve",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
}


def _venues() -> dict[str, str]:
    out = dict(KNOWN_VENUE_PROGRAMS)
    for entry in os.environ.get("SOLANA_VENUES", "").split(";"):
        name, _, prog = entry.partition(":")
        if name.strip() and prog.strip():
            out[prog.strip()] = name.strip()
    return out


class NoPairError(RuntimeError):
    pass


@dataclass
class SolBuyer:
    wallet: str
    slot: int
    seconds_after_creation: float
    sol_spent: float
    tokens_bought: int
    prior_txs: int | None = None       # activity before launch; None = unknown
    funder: str | None = None
    retained_pct: float | None = None


@dataclass
class SolLaunch:
    pool: str
    venue: str
    creation_slot: int = 0
    creation_time: int = 0
    history_complete: bool = True
    buyers: list[SolBuyer] = field(default_factory=list)
    creation_slot_buyers: int = 0
    sells_in_window: int = 0


def _fee_payer(tx: dict) -> str | None:
    keys = tx["transaction"]["message"].get("accountKeys") or []
    if not keys:
        return None
    k = keys[0]
    return k.get("pubkey") if isinstance(k, dict) else k


def _token_delta(tx: dict, mint: str, owner: str) -> int:
    """Raw token delta for `owner` in `mint` across the transaction."""
    meta = tx.get("meta") or {}
    def total(entries):
        return sum(int(e["uiTokenAmount"]["amount"]) for e in entries or []
                   if e.get("mint") == mint and e.get("owner") == owner)
    return total(meta.get("postTokenBalances")) - total(meta.get("preTokenBalances"))


def _sol_spent(tx: dict) -> float:
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if not pre or not post:
        return 0.0
    return max(0.0, (pre[0] - post[0]) / LAMPORTS)


def analyze_launch(rpc: SolRpc, mint: str, pool: str) -> SolLaunch:
    venue_map = _venues()
    pool_acc = rpc.account_info(pool) or {}
    prog = pool_acc.get("owner", "")
    launch = SolLaunch(pool=pool,
                       venue=venue_map.get(prog, f"unknown program {prog}"))

    sigs, complete = rpc.signatures_full(pool)
    launch.history_complete = complete
    if not sigs or not complete:
        return launch                    # never fake a launch window
    first = sigs[0]
    launch.creation_slot = first.get("slot") or 0
    launch.creation_time = first.get("blockTime") or 0
    if not launch.creation_time:
        return launch

    horizon = launch.creation_time + config.EARLY_WINDOW_SECONDS
    early = [s for s in sigs
             if (s.get("blockTime") or 0) <= horizon and not s.get("err")]
    early = early[: config.MAX_EARLY_BUYERS * 2]
    txs = rpc.transactions([s["signature"] for s in early])

    seen: dict[str, SolBuyer] = {}
    for sig, tx in zip(early, txs):
        if not tx or (tx.get("meta") or {}).get("err"):
            continue
        payer = _fee_payer(tx)
        if not payer:
            continue
        delta = _token_delta(tx, mint, payer)
        if delta > 0:
            b = seen.get(payer)
            if b is None:
                b = seen[payer] = SolBuyer(
                    wallet=payer, slot=sig.get("slot") or 0,
                    seconds_after_creation=float(
                        (sig.get("blockTime") or 0) - launch.creation_time),
                    sol_spent=0.0, tokens_bought=0)
            b.sol_spent += _sol_spent(tx)
            b.tokens_bought += delta
        elif delta < 0:
            launch.sells_in_window += 1
    launch.buyers = sorted(seen.values(), key=lambda b: b.slot)
    launch.creation_slot_buyers = sum(
        1 for b in launch.buyers if b.slot == launch.creation_slot)
    return launch


def trace_buyers(rpc: SolRpc, launch: SolLaunch, mint: str) -> None:
    """Fresh-wallet, funding-source, and offload facts per early buyer."""
    for b in launch.buyers[: config.MAX_EARLY_BUYERS]:
        try:
            page = rpc.signatures(b.wallet, limit=1000)
        except Exception:
            continue
        if len(page) >= 1000:
            b.prior_txs = 1000           # established wallet — not fresh,
            continue                     # funding hop is old news, skip
        b.prior_txs = sum(1 for s in page
                          if (s.get("blockTime") or 0) < launch.creation_time)
        earliest = page[-1]["signature"] if page else None
        if earliest:
            try:
                tx = rpc.transactions([earliest])[0]
            except Exception:
                tx = None
            if tx:
                b.funder = _inbound_funder(tx, b.wallet)
    # offload — current balance vs bought
    for b in launch.buyers[: config.MAX_EARLY_BUYERS]:
        if b.tokens_bought <= 0:
            continue
        try:
            bal = rpc.token_balance(b.wallet, mint)
        except Exception:
            continue
        b.retained_pct = min(100.0, 100.0 * bal / b.tokens_bought)


def _inbound_funder(tx: dict, wallet: str) -> str | None:
    """Source of the earliest inbound SOL transfer (System program)."""
    meta = tx.get("meta") or {}
    groups = [tx["transaction"]["message"].get("instructions") or []]
    for inner in meta.get("innerInstructions") or []:
        groups.append(inner.get("instructions") or [])
    for instrs in groups:
        for ins in instrs:
            parsed = ins.get("parsed")
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            if (parsed.get("type") in ("transfer", "createAccount")
                    and (info.get("destination") == wallet
                         or info.get("newAccount") == wallet)):
                src = info.get("source")
                if src and src != wallet:
                    return src
    return None


def holder_snapshot(rpc: SolRpc, mint_info, pool: str) -> dict:
    """Top-20 concentration. Solana RPC has no cheap full holder count —
    holder_count is None, and the note says so; never a made-up number."""
    largest = rpc.token_largest(mint_info.mint)
    accounts = [e["address"] for e in largest]
    owners_raw = rpc.multiple_accounts(accounts) if accounts else []
    rows = []           # (owner, raw_amount)
    pool_held = burned = 0
    for entry, acc in zip(largest, owners_raw):
        amt = int(entry.get("amount") or 0)
        owner = ""
        try:
            owner = acc["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            pass
        if entry["address"] == pool or owner == pool:
            pool_held += amt
        elif owner == BURN_OWNER:
            burned += amt
        else:
            rows.append((owner or entry["address"], amt))
    supply = mint_info.supply
    circulating = max(supply - pool_held - burned, 1)
    top10 = sum(a for _, a in rows[:10])
    return {"holder_count": None,
            "top10_pct": 100.0 * top10 / circulating,
            "burned_pct": 100.0 * burned / max(supply, 1),
            "pool_held_pct": 100.0 * pool_held / max(supply, 1),
            "top_rows": [(o, a) for o, a in rows[:10]],
            "top20_only": True}


def evaluate(mint_info, launch: SolLaunch | None, holders: dict | None) -> Verdict:
    v = Verdict()
    if mint_info.mint_authority:
        v.add(2, f"mint authority ACTIVE ({mint_info.mint_authority}) — "
                 "holder can print unlimited supply", "sol-mint-authority")
    if mint_info.freeze_authority:
        v.add(3, f"freeze authority ACTIVE ({mint_info.freeze_authority}) — "
                 "holder can freeze any wallet's tokens (honeypot lever)",
              "sol-freeze-authority")
    if mint_info.has_extensions:
        v.add(1, "Token-2022 with extensions — transfer fees/hooks possible, "
                 "read the extension set manually", "sol-token2022-extensions")

    if launch is None or not launch.history_complete:
        v.notes.append("pool history too deep to page to creation — launch "
                       "window NOT analyzed (UNRESOLVED, not passed)")
    elif launch.buyers:
        n = len(launch.buyers)
        if launch.creation_slot_buyers >= 3:
            v.add(3, f"{launch.creation_slot_buyers} wallets bought in the "
                     "pool-creation slot itself — bundled launch",
                  "creation-block-bundle")
        elif launch.creation_slot_buyers >= 1:
            v.add(1, f"{launch.creation_slot_buyers} buy(s) in the creation slot",
                  "creation-block-buy")
        dense = [b for b in launch.buyers if b.seconds_after_creation <= 5]
        if len(dense) >= 8:
            v.add(2, f"{len(dense)} distinct wallets bought within 5s of "
                     "pool creation", "early-buyer-density")

        traced = [b for b in launch.buyers if b.funder]
        clusters: dict[str, list[str]] = defaultdict(list)
        for b in traced:
            clusters[b.funder].append(b.wallet)
        clusters = {f: ws for f, ws in clusters.items() if len(ws) >= 2}
        if clusters:
            big = max(clusters.items(), key=lambda kv: len(kv[1]))
            v.add(3, f"{len(big[1])} early buyers share funder {big[0]} "
                     f"({len(clusters)} funding cluster(s) total)",
                  "funding-cluster")
        fresh = [b for b in launch.buyers
                 if b.prior_txs is not None and b.prior_txs <= 3]
        if len(fresh) / n >= 0.5:
            v.add(2, f"{len(fresh)}/{n} early buyers had ≤3 transactions "
                     "before launch (fresh wallets)", "fresh-wallets")
        offl = [b for b in launch.buyers
                if b.retained_pct is not None and b.retained_pct < 10]
        measured = [b for b in launch.buyers if b.retained_pct is not None]
        if measured and len(offl) / len(measured) >= 0.5:
            v.add(3, f"{len(offl)}/{len(measured)} early buyers already "
                     "offloaded (<10% of buy retained)", "sniper-offload")
        untraced = n - len([b for b in launch.buyers if b.prior_txs is not None])
        if untraced:
            v.notes.append(f"{untraced} early buyer(s) not traced (RPC "
                           "errors) — funding/freshness partly blind")
    else:
        v.notes.append("no buys found in the launch window")

    if holders:
        if holders["top10_pct"] >= 50:
            v.add(3, f"top 10 non-pool accounts hold {holders['top10_pct']:.1f}% "
                     "of circulating supply", "top10-concentration-high")
        elif holders["top10_pct"] >= 30:
            v.add(1, f"top 10 non-pool accounts hold {holders['top10_pct']:.1f}%",
                  "top10-concentration-mid")
        v.notes.append("holder stats cover the top-20 token accounts only "
                       "(Solana RPC has no cheap full holder count)")
    return v


def collect(mint: str, log: bool = True) -> dict:
    timings: dict[str, float] = {}
    t0 = time.time()
    pairs = [p for p in dexscreener.token_pairs(mint)
             if p.get("chainId") == DEX_CHAIN]
    if not pairs:
        raise NoPairError(f"DexScreener has no solana pair for {mint} — "
                          "token unindexed or address wrong")
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    pool = best.get("pairAddress") or ""
    market = {"priceUsd": best.get("priceUsd"),
              "liquidity": best.get("liquidity"),
              "dexId": best.get("dexId"),
              "quoteSymbol": (best.get("quoteToken") or {}).get("symbol"),
              "pairCreatedAt": best.get("pairCreatedAt")}
    timings["market"] = round(time.time() - t0, 2)

    rpc = SolRpc()
    t = time.time()
    acc = rpc.account_info(mint)
    if acc is None:
        raise NoPairError(f"{mint} is not an account on this RPC's chain")
    mint_info = parse_mint(mint, acc)
    timings["mint"] = round(time.time() - t, 2)

    t = time.time()
    launch = analyze_launch(rpc, mint, pool) if pool else None
    timings["launch"] = round(time.time() - t, 2)
    if launch and launch.history_complete and launch.buyers:
        t = time.time()
        trace_buyers(rpc, launch, mint)
        timings["buyers"] = round(time.time() - t, 2)

    t = time.time()
    holders = holder_snapshot(rpc, mint_info, pool) if pool else None
    timings["holders"] = round(time.time() - t, 2)

    verdict = evaluate(mint_info, launch, holders)
    payload = {
        "chain": CHAIN_KEY,
        "token": {"address": mint, "name": (best.get("baseToken") or {}).get("name", "?"),
                  "symbol": (best.get("baseToken") or {}).get("symbol", "?"),
                  "decimals": mint_info.decimals,
                  "mint_authority": mint_info.mint_authority,
                  "freeze_authority": mint_info.freeze_authority,
                  "token_2022": mint_info.is_token_2022},
        "pair": pool, "market": market,
        "launch": asdict(launch) if launch else None,
        "holders": holders,
        "verdict": asdict(verdict), "verdict_line": verdict_line(verdict),
        "timings": timings,
    }
    if log:
        try:
            store.log_run(token=mint, pair=pool, kind="report",
                          score=verdict.score,
                          flags=payload["verdict"]["flags"],
                          notes=verdict.notes, market=market,
                          data={"launch": payload["launch"],
                                "holders": holders},
                          chain=CHAIN_KEY)
        except Exception as exc:
            print(f"note: run ledger write failed ({exc})", file=sys.stderr)
    return payload


def render_text(d: dict) -> str:
    t, v = d["token"], d["verdict"]
    out = [f"=== {t['name']} ({t['symbol']}) — {t['address']} [solana] ===",
           f"pool: {d['pair']}  venue: {(d['launch'] or {}).get('venue', '?')}"
           + (f"  quote: {d['market'].get('quoteSymbol')}"
              if d['market'].get('quoteSymbol') else ""),
           f"price ${d['market'].get('priceUsd')}  "
           f"liquidity ${((d['market'].get('liquidity') or {}).get('usd'))}",
           # state the measured authority facts even when clean — a silent
           # absence of flags is not the same as showing what was checked
           "mint authority: " + (t.get("mint_authority") or "revoked (supply fixed)"),
           "freeze authority: " + (t.get("freeze_authority")
                                   or "revoked (accounts unfreezable)"),
           "",
           f"score {v['score']} — {d['verdict_line']}", ""]
    out += [f"[+{f['points']}] {f['label']}" for f in v["flags"]]
    out += [f"[note] {n}" for n in v["notes"]]
    la = d.get("launch") or {}
    if la.get("buyers") is not None:
        out += ["", f"launch: {la.get('creation_slot_buyers', 0)} creation-slot "
                    f"buys, {len(la.get('buyers') or [])} early buyers, "
                    f"{la.get('sells_in_window', 0)} sells in window"]
    ho = d.get("holders")
    if ho:
        out.append(f"holders (top-20 accounts): top10 {ho['top10_pct']:.1f}%, "
                   f"pool holds {ho['pool_held_pct']:.1f}%, "
                   f"burned {ho['burned_pct']:.1f}%")
    if d.get("timings"):
        out.append("timings: " + ", ".join(
            f"{k} {s}s" for k, s in sorted(d["timings"].items(),
                                           key=lambda kv: -kv[1])))
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) != 2 or not b58.looks_like_address(sys.argv[1]):
        print("usage: python -m src.solana.report <mint (base58)>")
        return 2
    try:
        payload = collect(sys.argv[1])
    except NoPairError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
