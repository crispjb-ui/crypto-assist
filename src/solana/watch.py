"""Watched-wallet tracker — real-time entries/exits for specific Solana
wallets (an operator's buyer + vault pair), surfaced in the dashboard with
desktop alerts.

.env:
    SOLANA_WATCH_WALLETS=buyer:7nqP...;vault:2E1g...;buyer-sep:Hukt...
Each scanner cycle polls each wallet's newest signatures since the last
seen one, parses every new transaction from balance deltas (same engine as
reconcile.py) and emits events: buy / sell / transfer_in / transfer_out /
protocol_in / protocol_out per mint. The first poll only sets the baseline
so a restart never floods alerts with history.
"""
from __future__ import annotations

import os
import sys
import time

from ..onchain import store
from . import b58
from .mint import parse_mint
from .reconcile import (SOL_NOISE, _dex_symbol, _keys, _label_currency,
                        _programs, _sym_cache, _token_deltas,
                        counterparty_type, LP_PROGRAMS)
from .rpc import SolRpc

MAX_NEW_PER_POLL = 40
_last_sig: dict[str, str] = {}
_events: list[dict] = []
_EVENTS_CAP = 300
_decimals: dict[str, int] = {}


def watched() -> list[tuple[str, str]]:
    """[(label, wallet)] from SOLANA_WATCH_WALLETS."""
    out = []
    for entry in os.environ.get("SOLANA_WATCH_WALLETS", "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        label, _, addr = entry.rpartition(":")
        addr = addr.strip()
        if b58.looks_like_address(addr):
            out.append((label.strip() or addr[:6], addr))
    return out


def _dec(rpc: SolRpc, mint: str) -> int:
    if mint not in _decimals:
        try:
            acc = rpc.account_info(mint)
            _decimals[mint] = parse_mint(mint, acc).decimals if acc else 0
        except Exception:
            _decimals[mint] = 0
    return _decimals[mint]


def _symbol(mint: str) -> str:
    if mint not in _sym_cache:
        _sym_cache[mint] = _dex_symbol(mint)
    return _sym_cache[mint] or mint[:4] + "…" + mint[-4:]


def parse_tx(rpc: SolRpc, wallet: str, label: str, sig: dict, tx: dict) -> list[dict]:
    """Events for one transaction, one per mint the wallet's balance moved in."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    keys = _keys(tx)
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol = (post[i] - pre[i]) / 1e9
    deltas = _token_deltas(tx)
    mine = {m: v for (o, m), v in deltas.items() if o == wallet and v}
    progs = _programs(tx)
    venues = [LP_PROGRAMS[p] for p in progs if p in LP_PROGRAMS]
    out = []
    ts = sig.get("blockTime") or 0
    from .reconcile import STAKING, AGGREGATORS
    staking = any(v in STAKING for v in venues)
    routed = any(v in AGGREGATORS for v in venues)   # Jupiter route = a fill
    for mint, d in mine.items():
        others = {o: v for (o, m), v in deltas.items() if m == mint and o != wallet and v}
        flows = {c: v for c, v in
                 [("SOL", sol)] + [(_label_currency(m2, {}), v2)
                                    for m2, v2 in mine.items() if m2 != mint]
                 if abs(v) > (SOL_NOISE if c == "SOL" else 0)}
        q_out = any(v < 0 for v in flows.values())
        q_in = any(v > 0 for v in flows.values())
        if d > 0:
            kind = ("buy" if q_out else "unstake" if staking
                    else "fill" if routed else "transfer_in")
        else:
            kind = ("sell" if q_in else "stake" if staking
                    else "fill_out" if routed else "transfer_out")
        cp = ""
        if kind in ("transfer_in", "transfer_out") and others:
            want = (lambda v: v < 0) if kind == "transfer_in" else (lambda v: v > 0)
            c = [o for o, v in others.items() if want(v)]
            cp = c[0] if c else ""
            if cp and counterparty_type(rpc, cp) == "protocol":
                kind = "protocol_in" if kind == "transfer_in" else "protocol_out"
        out.append({"ts": ts, "wallet": wallet, "label": label, "kind": kind,
                    "mint": mint, "symbol": _symbol(mint), "amount": d,
                    "flows": flows, "counterparty": cp, "venues": venues,
                    "sig": sig["signature"]})
    return out


def poll(rpc: SolRpc | None = None) -> list[dict]:
    """New events across all watched wallets since the previous poll."""
    targets = watched()
    if not targets or not os.environ.get("SOLANA_RPC_URL"):
        return []
    rpc = rpc or SolRpc()
    fresh: list[dict] = []
    for label, wallet in targets:
        try:
            sigs = rpc.signatures(wallet, limit=MAX_NEW_PER_POLL)
        except Exception as exc:
            print(f"watch: {label} signatures failed ({exc})", file=sys.stderr)
            continue
        if not sigs:
            continue
        last = _last_sig.get(wallet)
        if last is None:                       # baseline only, no history flood
            _last_sig[wallet] = sigs[0]["signature"]
            continue
        new = []
        for sg in sigs:
            if sg["signature"] == last:
                break
            new.append(sg)
        if not new:
            continue
        _last_sig[wallet] = sigs[0]["signature"]
        new.reverse()                           # oldest first
        try:
            txs = rpc.transactions([sg["signature"] for sg in new])
        except Exception as exc:
            print(f"watch: {label} fetch failed ({exc})", file=sys.stderr)
            continue
        for sg, tx in zip(new, txs):
            if not tx:
                tx = rpc.transaction(sg["signature"])
            if tx:
                fresh.extend(parse_tx(rpc, wallet, label, sg, tx))
    if fresh:
        _events.extend(fresh)
        del _events[:-_EVENTS_CAP]
    return fresh


def recent(limit: int = 100) -> list[dict]:
    return list(reversed(_events[-limit:]))


def describe(e: dict) -> str:
    amt = f"{abs(e['amount']):,.2f} {e['symbol']}"
    fl = " + ".join(f"{abs(v):,.4g} {c}" for c, v in e["flows"].items())
    k = e["kind"]
    if k == "buy":
        return f"{e['label']} BOUGHT {amt}" + (f" for {fl}" if fl else "")
    if k == "sell":
        return f"{e['label']} SOLD {amt}" + (f" for {fl}" if fl else "")
    if k == "transfer_out":
        return f"{e['label']} SENT {amt} → {e['counterparty'][:8]}…"
    if k == "transfer_in":
        return f"{e['label']} received {amt} ← {e['counterparty'][:8]}…"
    if k == "fill":
        return f"{e['label']} BOUGHT {amt} (DCA/order fill)" + (f" via {', '.join(e['venues'])}" if e["venues"] else "")
    if k == "fill_out":
        return f"{e['label']} SOLD {amt} (DCA/order fill)" + (f" via {', '.join(e['venues'])}" if e["venues"] else "")
    if k == "stake":
        return f"{e['label']} STAKED {amt}"
    if k == "unstake":
        return f"{e['label']} UNSTAKED {amt} — pre-exit tell"
    if k == "protocol_out":
        return f"{e['label']} posted/escrowed {amt}" + (f" ({', '.join(e['venues'])})" if e["venues"] else "")
    if k == "protocol_in":
        return f"{e['label']} fill/claim/cancel {amt}" + (f" ({', '.join(e['venues'])})" if e["venues"] else "")
    return f"{e['label']} {k} {amt}"
