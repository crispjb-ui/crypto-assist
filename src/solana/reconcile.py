"""Wallet × token reconciliation — every movement of one token through one
wallet, from account creation to now, with cash flows kept per currency.

For a TOKEN (mint): reconciles the key actors — pool creator, top holders,
biggest early buyers — and ranks them by what they extracted.
For a WALLET: reconciles that wallet in one mint (--mint) or in every mint it
currently holds.

    python -m src.solana.reconcile <mint-or-wallet> [--mint M] [--wallets N]

Method: the wallet's associated token account (ATA) for the mint is derived
(pda.py), its full signature history pulled (works even if the account was
closed), every transaction parsed from balance deltas (never from program
names alone), and each movement classified: buy / sell / transfer in / out /
LP deposit / LP withdraw / burn. The ledger must reconcile:
  bought + received + LP-withdrawn − sold − sent − LP-deposited − burned
  == current balance; any remainder is reported as UNRECONCILED, not hidden.
Cash flows are reported per currency (SOL, USDC, ...) and never converted —
SOL figures include fees and rent effects, exactly as the chain records them.
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from ..onchain import dexscreener, store
from . import b58
from .mint import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, parse_mint
from .pda import associated_token_address
from .report import (BURN_OWNER, DEX_CHAIN, KNOWN_VENUE_PROGRAMS, WSOL_MINT,
                     _fee_payer)
from .rpc import SolRpc

SYSTEM_PROGRAM = "11111111111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Liquidity/DEX programs labeled ONLY on exact match (ids verified in use);
# anything else is shown raw. Extend via SOLANA_VENUES like report.py.
LP_PROGRAMS = dict(KNOWN_VENUE_PROGRAMS)
LP_PROGRAMS.update({
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora Dynamic AMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
})
SOL_NOISE = 0.005          # SOL moves below this are fees/rent, not a trade
MAX_SIG_PAGES = 5          # 5,000 signatures per token account
MAX_TXS = 600              # transactions fetched per wallet×mint
DEFAULT_WALLETS = 6


@dataclass
class Event:
    sig: str
    ts: int
    kind: str                   # buy|sell|transfer_in|transfer_out|lp_deposit|lp_withdraw|burn|other
    tokens: float               # signed token delta (ui units)
    flows: dict = field(default_factory=dict)   # currency -> signed amount
    counterparty: str = ""
    programs: list = field(default_factory=list)


@dataclass
class WalletLedger:
    wallet: str
    mint: str
    symbol: str = "?"
    role: str = ""
    ata: str = ""
    owner_type: str = "wallet"     # wallet | program-owned | unknown
    accounts: list = field(default_factory=list)   # token accounts covered
    history_complete: bool = True
    txs_seen: int = 0
    txs_fetched: int = 0
    txs_unfetchable: int = 0       # RPC returned nothing for these
    txs_failed: int = 0            # on-chain failed transactions, ignored
    notes: list = field(default_factory=list)
    events: list = field(default_factory=list)
    bought: float = 0.0
    sold: float = 0.0
    received: float = 0.0
    sent: float = 0.0
    lp_deposited: float = 0.0
    lp_withdrawn: float = 0.0
    burned: float = 0.0
    buys: int = 0
    sells: int = 0
    outlay: dict = field(default_factory=dict)      # currency -> spent on buys
    receipts: dict = field(default_factory=dict)    # currency -> received on sells
    net_flow: dict = field(default_factory=dict)    # currency -> all events
    balance_now: float = 0.0
    unreconciled: float = 0.0
    first_ts: int = 0
    last_ts: int = 0

    def expected_balance(self) -> float:
        return (self.bought + self.received + self.lp_withdrawn
                - self.sold - self.sent - self.lp_deposited - self.burned)


# ----------------------------------------------------------------- helpers
def _keys(tx: dict) -> list[str]:
    ks = tx["transaction"]["message"].get("accountKeys") or []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in ks]


def _programs(tx: dict) -> set[str]:
    out: set[str] = set()
    msg = tx["transaction"]["message"]
    for ins in msg.get("instructions") or []:
        if ins.get("programId"):
            out.add(ins["programId"])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ins in inner.get("instructions") or []:
            if ins.get("programId"):
                out.add(ins["programId"])
    return out


def _parsed_token_instructions(tx: dict):
    msg = tx["transaction"]["message"]
    groups = [msg.get("instructions") or []]
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        groups.append(inner.get("instructions") or [])
    for instrs in groups:
        for ins in instrs:
            if ins.get("program") in ("spl-token", "spl-token-2022") \
                    and isinstance(ins.get("parsed"), dict):
                yield ins["parsed"]


def _token_deltas(tx: dict) -> dict[tuple[str, str], float]:
    """(owner, mint) -> signed ui delta across the transaction."""
    meta = tx.get("meta") or {}
    agg: dict[tuple[str, str], float] = defaultdict(float)
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for e in meta.get(key) or []:
            owner, mint = e.get("owner"), e.get("mint")
            if not (owner and mint):
                continue
            ui = e.get("uiTokenAmount") or {}
            amt = int(ui.get("amount") or 0) / 10 ** int(ui.get("decimals") or 0)
            agg[(owner, mint)] += sign * amt
    return agg


def _burned_by(tx: dict, wallet: str, mint: str, decimals: int) -> float:
    total = 0
    for p in _parsed_token_instructions(tx):
        if p.get("type") in ("burn", "burnChecked"):
            info = p.get("info") or {}
            if info.get("mint") == mint and info.get("authority") == wallet:
                ta = info.get("tokenAmount") or {}
                total += int(ta.get("amount") or info.get("amount") or 0)
    return total / 10 ** decimals


def _label_currency(mint: str, symbols: dict[str, str]) -> str:
    if mint == "SOL" or mint == WSOL_MINT:
        return "SOL"
    if mint == USDC_MINT:
        return "USDC"
    return symbols.get(mint) or mint[:4] + "…" + mint[-4:]


# ------------------------------------------------------------- the engine
def classify_address(rpc: SolRpc, address: str) -> tuple[str, dict | None]:
    """'mint' | 'wallet' | 'token-account' | 'program' | 'unknown'."""
    acc = rpc.account_info(address)
    if acc is None:
        return "unknown", None
    owner = acc.get("owner", "")
    data = acc.get("data")
    raw = base64.b64decode(data[0]) if isinstance(data, list) else b""
    if owner == SYSTEM_PROGRAM and not acc.get("executable"):
        return "wallet", acc
    if owner in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        if len(raw) == 165 or (len(raw) > 165 and raw[165] == 2):
            return "token-account", acc
        return "mint", acc
    if acc.get("executable"):
        return "program", acc
    return "unknown", acc


def reconcile_wallet(rpc: SolRpc, wallet: str, mint: str, mint_info,
                     symbols: dict[str, str] | None = None,
                     role: str = "") -> WalletLedger:
    symbols = symbols or {}
    led = WalletLedger(wallet=wallet, mint=mint, role=role)
    token_program = mint_info.owner_program or TOKEN_PROGRAM
    led.ata = associated_token_address(wallet, mint, token_program)
    dec = mint_info.decimals

    # who owns the tokens: a human-controlled wallet (System-owned account)
    # or a program-owned account / PDA (pool vault, locker, staking)? A PDA
    # authority often has no account at all — that is "program-owned" too.
    try:
        kind, acc = classify_address(rpc, wallet)
        led.owner_type = "wallet" if kind == "wallet" else "program-owned"
    except Exception:
        led.owner_type = "unknown"
    if led.owner_type != "wallet":
        led.notes.append("owner is not a System-owned wallet — a program-"
                         "owned account/PDA (pool vault, locker, staking); "
                         "its flows are protocol mechanics, not a trader")

    # every token account this owner has for the mint (open ones) plus the
    # derived ATA (reachable even if closed); histories merged
    accounts = {led.ata}
    try:
        for a in rpc.token_accounts(wallet):
            if a["mint"] == mint:
                accounts.add(a["pubkey"])
    except Exception:
        pass
    led.accounts = sorted(accounts)
    sigs: list[dict] = []
    seen_sigs: set[str] = set()
    for acct in led.accounts:
        try:
            part, complete = rpc.signatures_full(acct, max_pages=MAX_SIG_PAGES)
        except Exception:
            continue
        led.history_complete = led.history_complete and complete
        for sg in part:
            if sg["signature"] not in seen_sigs:
                seen_sigs.add(sg["signature"]); sigs.append(sg)
    sigs.sort(key=lambda sg: (sg.get("blockTime") or 0, sg.get("slot") or 0))
    led.txs_failed = sum(1 for sg in sigs if sg.get("err"))
    sigs = [sg for sg in sigs if not sg.get("err")]
    led.txs_seen = len(sigs)
    if len(sigs) > MAX_TXS:
        sigs = sigs[-MAX_TXS:]            # newest window; flagged in output
        led.history_complete = False
    led.txs_fetched = len(sigs)
    txs = rpc.transactions([sg["signature"] for sg in sigs]) if sigs else []
    led.txs_unfetchable = sum(1 for t in txs if not t)
    if led.txs_unfetchable:
        led.notes.append(f"{led.txs_unfetchable}/{led.txs_fetched} transactions "
                         "returned nothing from the RPC (older history the "
                         "node does not serve) — totals exclude them")

    net: dict[str, float] = defaultdict(float)
    for sig, tx in zip(sigs, txs):
        if not tx or (tx.get("meta") or {}).get("err"):
            continue
        ts = sig.get("blockTime") or 0
        keys = _keys(tx)
        meta = tx.get("meta") or {}
        sol = 0.0
        if wallet in keys:
            i = keys.index(wallet)
            pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
            if i < len(pre) and i < len(post):
                sol = (post[i] - pre[i]) / 1e9
        deltas = _token_deltas(tx)
        d_tok = deltas.get((wallet, mint), 0.0)
        flows: dict[str, float] = {}
        if abs(sol) > 0:
            flows["SOL"] = sol
        for (own, m), v in deltas.items():
            if own == wallet and m != mint and v:
                flows[_label_currency(m, symbols)] = flows.get(
                    _label_currency(m, symbols), 0.0) + v
        progs = _programs(tx)
        lp = [LP_PROGRAMS[p] for p in progs if p in LP_PROGRAMS]
        burned = _burned_by(tx, wallet, mint, dec)
        quote_out = any(v < -SOL_NOISE if c == "SOL" else v < 0
                        for c, v in flows.items())
        quote_in = any(v > SOL_NOISE if c == "SOL" else v > 0
                       for c, v in flows.items())
        others = {own: v for (own, m), v in deltas.items()
                  if m == mint and own != wallet and v}

        if d_tok > 0:
            if quote_out:
                kind = "buy"
            elif lp:
                kind = "lp_withdraw"
            else:
                kind = "transfer_in"
        elif d_tok < 0:
            if burned and abs(abs(d_tok) - burned) < 10 ** -dec * 2:
                kind = "burn"
            elif quote_in:
                kind = "sell"
            elif lp:
                kind = "lp_deposit"
            else:
                kind = "transfer_out"
        else:
            kind = "other"
        cp = ""
        if kind in ("transfer_in", "transfer_out") and others:
            want = (lambda v: v < 0) if kind == "transfer_in" else (lambda v: v > 0)
            cands = [o for o, v in others.items() if want(v)]
            cp = cands[0] if cands else ""

        ev = Event(sig=sig["signature"], ts=ts, kind=kind, tokens=d_tok,
                   flows=flows, counterparty=cp, programs=lp)
        led.events.append(ev)
        for c, v in flows.items():
            net[c] += v
        if kind == "buy":
            led.buys += 1; led.bought += d_tok
            for c, v in flows.items():
                if v < 0:
                    led.outlay[c] = led.outlay.get(c, 0.0) - v
        elif kind == "sell":
            led.sells += 1; led.sold += -d_tok
            for c, v in flows.items():
                if v > 0:
                    led.receipts[c] = led.receipts.get(c, 0.0) + v
        elif kind == "transfer_in":
            led.received += d_tok
        elif kind == "transfer_out":
            led.sent += -d_tok
        elif kind == "lp_deposit":
            led.lp_deposited += -d_tok
        elif kind == "lp_withdraw":
            led.lp_withdrawn += d_tok
        elif kind == "burn":
            led.burned += -d_tok
        if ts:
            led.first_ts = min(led.first_ts or ts, ts)
            led.last_ts = max(led.last_ts, ts)
    led.net_flow = dict(net)
    try:
        led.balance_now = rpc.token_balance(wallet, mint) / 10 ** dec
    except Exception:
        led.balance_now = float("nan")
    led.unreconciled = led.balance_now - led.expected_balance()
    return led


def _symbol_map() -> dict[str, str]:
    try:
        return {addr: sym for sym, addr in store.quote_token_map(chain="solana").items()}
    except Exception:
        return {}


def key_wallets(rpc: SolRpc, mint: str, mint_info, limit: int,
                notes: list[str] | None = None) -> list[tuple[str, str]]:
    """(wallet, role) candidates for a mint: pool creator, top holders,
    biggest early buyers — deduped, capped. Gaps are written to `notes`."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    notes = notes if notes is not None else []

    def add(w: str, role: str) -> None:
        if w and w not in seen and len(out) < limit:
            seen.add(w); out.append((w, role))

    pairs = [p for p in dexscreener.token_pairs(mint) if p.get("chainId") == DEX_CHAIN]
    pool = ""
    if pairs:
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        pool = best.get("pairAddress") or ""
    else:
        notes.append("no DexScreener pool — pool creator and early buyers "
                     "cannot be identified")
    if pool:
        sigs, complete = rpc.signatures_full(pool, max_pages=3)
        if sigs and complete:
            first = rpc.transactions([sigs[0]["signature"]])[0]
            if first:
                add(_fee_payer(first) or "", "pool creator")
        else:
            notes.append("pool history deeper than 3,000 transactions — "
                         "pool creator not identified")
        from .report import analyze_launch
        try:
            launch = analyze_launch(rpc, mint, pool)
            if not launch.history_complete:
                notes.append("pool history too deep to page to creation — "
                             "early buyers not identified")
            for b in sorted(launch.buyers, key=lambda b: -b.sol_spent)[:3]:
                add(b.wallet, "early buyer")
        except Exception as exc:
            notes.append(f"early-buyer analysis failed ({exc})")
    largest = rpc.token_largest(mint)
    accounts = [e["address"] for e in largest]
    for entry, acc in zip(largest, rpc.multiple_accounts(accounts) if accounts else []):
        try:
            owner = acc["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            continue
        if owner in (pool, BURN_OWNER) or entry["address"] == pool:
            continue
        add(owner, "top holder")
    return out


def reconcile(rpc: SolRpc, address: str, mint: str | None = None,
              wallets: int = DEFAULT_WALLETS) -> dict:
    t0 = time.time()
    kind, acc = classify_address(rpc, address)
    symbols = _symbol_map()
    ledgers: list[WalletLedger] = []
    notes: list[str] = []
    if kind == "mint":
        mint_info = parse_mint(address, acc)
        sym = symbols.get(address) or _dex_symbol(address) or "?"
        for w, role in key_wallets(rpc, address, mint_info, wallets, notes):
            led = reconcile_wallet(rpc, w, address, mint_info, symbols, role)
            led.symbol = sym
            ledgers.append(led)
        subject = {"type": "mint", "address": address, "symbol": sym}
    elif kind == "wallet":
        targets = [mint] if mint else [
            a["mint"] for a in rpc.token_accounts(address)][:wallets]
        for m in targets:
            macc = rpc.account_info(m)
            if not macc:
                continue
            mi = parse_mint(m, macc)
            led = reconcile_wallet(rpc, address, m, mi, symbols, "wallet")
            led.symbol = symbols.get(m) or _dex_symbol(m) or "?"
            ledgers.append(led)
        subject = {"type": "wallet", "address": address}
    else:
        raise ValueError(f"{address} is a {kind}, not a token mint or wallet")
    ledgers.sort(key=lambda l: -sum(v for v in l.net_flow.values() if v > 0))
    return {"subject": subject, "ledgers": [asdict(l) for l in ledgers],
            "notes": notes, "elapsed": round(time.time() - t0, 1)}


def _dex_symbol(mint: str) -> str | None:
    try:
        pairs = [p for p in dexscreener.token_pairs(mint)
                 if p.get("chainId") == DEX_CHAIN]
        return (pairs[0].get("baseToken") or {}).get("symbol") if pairs else None
    except Exception:
        return None


# ------------------------------------------------------------- rendering
def _fmt(v: float) -> str:
    """Full token precision always — a 34,999,999.999999 withdrawal is a
    different fact from 35,000,000."""
    if v != v:                       # NaN: balance lookup failed
        return "unknown"
    return f"{v:,.6f}".rstrip("0").rstrip(".") or "0"


def _flows(d: dict) -> str:
    return " + ".join(f"{_fmt(v)} {c}" for c, v in sorted(d.items())) or "—"


def _when(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "?"


def render_ledger(l: dict) -> list[str]:
    sym = l["symbol"]
    kind = l.get("owner_type", "wallet")
    tag = "" if kind == "wallet" else f" — {kind.upper()} ACCOUNT, not a trader"
    accts = l.get("accounts") or [l["ata"]]
    parsed = l["txs_fetched"] - l.get("txs_unfetchable", 0)
    out = [f"--- {l['wallet']}  [{l['role']}{tag}]",
           f"    token account(s): {', '.join(accts)}"]
    for n in l.get("notes") or []:
        out.append(f"    [note] {n}")
    out += [f"    {parsed} transactions reconciled"
           + (f" ({l['txs_unfetchable']} UNFETCHABLE from RPC)"
              if l.get("txs_unfetchable") else "")
           + (f", {l['txs_failed']} failed on-chain ignored" if l.get("txs_failed") else "")
           + ("" if l["history_complete"] else
              f" (HISTORY TRUNCATED — {l['txs_seen']} seen; newest window only)")
           + (f", {_when(l['first_ts'])} → {_when(l['last_ts'])}" if l["first_ts"] else ""),
           f"    {l['buys']} purchases: {_fmt(l['bought'])} {sym}   outlay {_flows(l['outlay'])}",
           f"    {l['sells']} sales:     {_fmt(l['sold'])} {sym}   receipts {_flows(l['receipts'])}"]
    if l["received"] or l["sent"]:
        out.append(f"    transfers: +{_fmt(l['received'])} in, -{_fmt(l['sent'])} out {sym}")
    if l["lp_deposited"] or l["lp_withdrawn"]:
        out.append(f"    liquidity: {_fmt(l['lp_deposited'])} deposited, "
                   f"{_fmt(l['lp_withdrawn'])} withdrawn {sym}")
    if l["burned"]:
        out.append(f"    burned: {_fmt(l['burned'])} {sym}")
    out.append(f"    net cash flow (all events, fees/rent included): "
               f"{_flows(l['net_flow'])}")
    out.append(f"    holds now: {_fmt(l['balance_now'])} {sym}"
               + ("" if abs(l["unreconciled"]) < 1e-6 else
                  f"   UNRECONCILED: {_fmt(l['unreconciled'])} {sym} ("
                  + ("unfetchable transactions" if l.get("txs_unfetchable") else
                     "truncated history" if not l["history_complete"] else
                     "movements in accounts this owner no longer holds")
                  + ")"))
    notable = [e for e in l["events"]
               if e["kind"] in ("lp_deposit", "lp_withdraw", "burn",
                                "transfer_in", "transfer_out")]
    if notable:
        out.append("    notable movements:")
        for e in notable[:12]:
            out.append(f"      {_when(e['ts'])}  {e['kind']:<12} "
                       f"{_fmt(e['tokens'])} {sym}"
                       + (f"  via {', '.join(e['programs'])}" if e["programs"] else "")
                       + (f"  ↔ {e['counterparty']}" if e["counterparty"] else "")
                       + f"  {e['sig'][:16]}…")
        if len(notable) > 12:
            out.append(f"      … {len(notable) - 12} more")
    return out


def render_text(d: dict) -> str:
    s = d["subject"]
    head = (f"=== reconciliation — {s.get('symbol', '')} {s['address']} "
            f"[{s['type']}] — {len(d['ledgers'])} wallet(s), {d['elapsed']}s ===")
    body = [head, "cash flows are per currency, never converted; SOL includes "
                  "fees and rent effects"]
    body += [f"[note] {n}" for n in d.get("notes") or []]
    body.append("")
    for l in d["ledgers"]:
        body += render_ledger(l) + [""]
    if not d["ledgers"]:
        body.append("no wallets to reconcile (no pool found / no token accounts)")
    return "\n".join(body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("address")
    ap.add_argument("--mint", help="when address is a wallet: the token to reconcile")
    ap.add_argument("--wallets", type=int, default=DEFAULT_WALLETS)
    a = ap.parse_args()
    if not b58.looks_like_address(a.address):
        print("not a base58 Solana address", file=sys.stderr)
        return 2
    try:
        print(render_text(reconcile(SolRpc(), a.address, a.mint, a.wallets)))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
