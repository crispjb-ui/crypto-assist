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
    "DCA265Vj8a9CEuX1eb1LWRnDT7uK6q1xMipnNyatn23M": "Jupiter DCA",
    "j1o2qRpjcyUwEvwtcfhEQefh773ZgjxcVRry7LDqg5X": "Jupiter Limit Order",
})
_cp_type_cache: dict[str, str] = {}


def counterparty_type(rpc: SolRpc, address: str) -> str:
    """'wallet' (System-owned, a person can sign) or 'protocol' (program-
    owned account / PDA authority: DEX vault, DCA or limit-order engine,
    staking). Cached per process."""
    if address not in _cp_type_cache:
        try:
            kind, _ = classify_address(rpc, address)
            _cp_type_cache[address] = "wallet" if kind == "wallet" else "protocol"
        except Exception:
            _cp_type_cache[address] = "unknown"
    return _cp_type_cache[address]
SOL_NOISE = 0.005          # SOL moves below this are fees/rent, not a trade
RETRY_BUDGET = 8           # consecutive single-fetch failures before giving up
VERBOSE = False            # --debug: progress + diagnostics on stderr


def _log(msg: str) -> None:
    if VERBOSE:
        print(msg, file=sys.stderr, flush=True)
MAX_SIG_PAGES = 5          # 5,000 signatures per token account
MAX_TXS = 600              # transactions fetched per wallet×mint (--max-txs lifts)
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
    received: float = 0.0          # from WALLETS (transfers)
    sent: float = 0.0              # to WALLETS
    protocol_in: float = 0.0       # from protocol accounts: DCA/limit fills,
                                   # claims — the holder is the acquirer
    protocol_out: float = 0.0      # to protocol accounts: escrows, deposits
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
        return (self.bought + self.received + self.protocol_in + self.lp_withdrawn
                - self.sold - self.sent - self.protocol_out - self.lp_deposited
                - self.burned)


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


_sym_cache: dict[str, str | None] = {}
# tickers people trust on sight; on any mint but the canonical one the label
# must say the symbol is self-declared
_MAJORS = {"BTC", "WBTC", "ETH", "WETH", "SOL", "USDC", "USDT", "JUP"}
_CANONICAL = {USDC_MINT: "USDC", WSOL_MINT: "SOL",
              "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP"}


def _label_currency(mint: str, symbols: dict[str, str]) -> str:
    if mint == "SOL" or mint == WSOL_MINT:
        return "SOL"
    if mint == USDC_MINT:
        return "USDC"
    if mint in symbols:
        return symbols[mint]
    if mint not in _sym_cache:            # one DexScreener lookup per mint
        _sym_cache[mint] = _dex_symbol(mint)
    sym = _sym_cache[mint]
    if not sym:
        return mint[:4] + "…" + mint[-4:]
    if sym.upper() in _MAJORS and _CANONICAL.get(mint) != sym.upper():
        return f"'{sym}' UNVERIFIED SYMBOL ({mint[:4]}…{mint[-4:]})"
    return f"{sym} ({mint[:4]}…{mint[-4:]})"


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
        accounts.update(rpc.token_accounts_for_mint(wallet, mint))
    except Exception as exc:
        led.notes.append(f"could not list token accounts ({str(exc)[:80]}) "
                         "— derived ATA only")
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
    rpc.last_errors = []
    _log(f"  {wallet[:8]}… {mint[:6]}…: {len(led.accounts)} account(s), "
         f"{led.txs_fetched} txs to fetch")
    txs = rpc.transactions([sg["signature"] for sg in sigs]) if sigs else []
    empties = [i for i, t in enumerate(txs) if not t]
    if empties:
        _log(f"    batch returned {len(empties)} empty — retrying singly "
             f"(budget {RETRY_BUDGET} consecutive failures)")
    # batch items can come back empty under rate limiting / batch rejection;
    # retry those one at a time, but a node that keeps failing must not stall
    # the whole run — stop after RETRY_BUDGET consecutive failures
    consecutive = 0
    for n, i in enumerate(empties, 1):
        if consecutive >= RETRY_BUDGET:
            break
        txs[i] = rpc.transaction(sigs[i]["signature"])
        consecutive = 0 if txs[i] else consecutive + 1
        if n % 10 == 0:
            _log(f"    retried {n}/{len(empties)}")
    led.txs_unfetchable = sum(1 for t in txs if not t)
    if led.txs_unfetchable:
        why = rpc.last_errors[-1] if rpc.last_errors else "no error text returned"
        samples = [sigs[i]["signature"] for i, t in enumerate(txs) if not t][:3]
        led.notes.append(f"{led.txs_unfetchable}/{led.txs_fetched} transactions "
                         f"unfetchable — RPC said: {why}"
                         + (" (retry budget exhausted)" if consecutive >= RETRY_BUDGET else "")
                         + " — totals exclude them; sample signatures: "
                         + ", ".join(samples))
        if VERBOSE and samples:
            _log(f"    probing {samples[0][:16]}…:")
            for k, v in rpc.probe_transaction(samples[0]).items():
                _log(f"      {k:<24} {v}")

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
        if not lp:   # name the unlabeled programs so they can be registered
            skip = {SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM,
                    "ComputeBudget111111111111111111111111111111",
                    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
                    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"}
            unlabeled = [f"program {q[:8]}…{q[-4:]}" for q in sorted(progs)
                         if q not in skip][:2]
        else:
            unlabeled = []
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
            if cp and counterparty_type(rpc, cp) == "protocol":
                # tokens arriving from / leaving to a program-owned account
                # with no payment leg = order fill, claim, escrow, deposit —
                # not a wallet-to-wallet transfer
                kind = "protocol_in" if kind == "transfer_in" else "protocol_out"

        ev = Event(sig=sig["signature"], ts=ts, kind=kind, tokens=d_tok,
                   flows=flows, counterparty=cp,
                   programs=lp or (unlabeled if kind.startswith(("lp_", "protocol_")) else []))
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
        elif kind == "protocol_in":
            led.protocol_in += d_tok
        elif kind == "protocol_out":
            led.protocol_out += -d_tok
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
    _flag_lookalikes(led)
    try:
        led.balance_now = rpc.token_balance(wallet, mint) / 10 ** dec
    except Exception:
        led.balance_now = float("nan")
    led.unreconciled = led.balance_now - led.expected_balance()
    return led


LOOKALIKE_PREFIX = 4


def _flag_lookalikes(led: WalletLedger) -> None:
    """Address poisoning: a dust transfer from an address sharing the leading
    characters of a REAL counterparty, timed around real transfers, so a
    copy-paste from history goes to the attacker. Flagged, never silently
    merged with the genuine counterparty."""
    real: dict[str, float] = defaultdict(float)
    for e in led.events:
        if e.kind in ("transfer_in", "transfer_out") and e.counterparty:
            real[e.counterparty] += abs(e.tokens)
    if len(real) < 2:
        return
    biggest = max(real.values())
    for cp, vol in real.items():
        if vol >= biggest * 1e-6:
            continue                     # not dust relative to real flows
        twins = [o for o in real if o != cp and o[:LOOKALIKE_PREFIX] == cp[:LOOKALIKE_PREFIX]
                 and real[o] > vol * 1000]
        if twins:
            led.notes.append(
                f"ADDRESS POISONING: dust from {cp} mimics real counterparty "
                f"{twins[0]} (same leading {LOOKALIKE_PREFIX} chars) — never copy "
                "this address from history")
            for e in led.events:
                if e.counterparty == cp:
                    e.kind = "lookalike_dust"


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


def resolve_mint(rpc: SolRpc, wallet: str, mint_or_symbol: str) -> str:
    """Accept a full mint or a ticker; a ticker is matched against the
    symbols of the wallet's own token accounts (DexScreener lookup, cached).
    Ambiguous or unknown tickers raise with the candidates listed."""
    if b58.looks_like_address(mint_or_symbol):
        return mint_or_symbol
    want = mint_or_symbol.upper().lstrip("$")
    hits = []
    for a in rpc.token_accounts(wallet):
        m = a["mint"]
        if m not in _sym_cache:
            _sym_cache[m] = _dex_symbol(m)
        if (_sym_cache[m] or "").upper() == want:
            hits.append(m)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no token account of {wallet} has symbol {want} "
                         "(closed accounts can't be listed — pass the full mint)")
    raise ValueError(f"symbol {want} matches several mints: {', '.join(hits)} "
                     "— pass the full mint")


def reconcile(rpc: SolRpc, address: str, mint: str | None = None,
              wallets: int = DEFAULT_WALLETS) -> dict:
    t0 = time.time()
    kind, acc = classify_address(rpc, address)
    if mint and kind == "wallet":
        mint = resolve_mint(rpc, address, mint)
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


# ------------------------------------------------- feeder (hot→cold) check
def feeder_check(rpc: SolRpc, holder: str, mint: str | None = None,
                 max_mints: int = 10) -> dict:
    """Is `holder` a holding wallet fed by a trading wallet? For each mint
    the holder has: who sent it tokens, did that sender BUY them on the
    market, and do sent/received amounts match. Also whether the holder's
    first SOL came from that sender (same-operator evidence)."""
    symbols = _symbol_map()
    if mint:
        mint = resolve_mint(rpc, holder, mint)
    held = [] if mint else [a["mint"] for a in rpc.token_accounts(holder)]
    mints = [mint] if mint else held[:max_mints]
    skipped = max(0, len(held) - len(mints))
    rows = []
    for m in mints:
        macc = rpc.account_info(m)
        if not macc:
            continue
        try:
            mi = parse_mint(m, macc)
        except ValueError:
            continue
        sym = symbols.get(m) or _dex_symbol(m) or m[:4] + "…"
        led_h = reconcile_wallet(rpc, holder, m, mi, symbols, "holder")
        feeders: dict[str, float] = defaultdict(float)
        for e in led_h.events:
            if e.kind == "transfer_in" and e.counterparty:
                feeders[e.counterparty] += e.tokens
        if not feeders:
            if led_h.protocol_in and not led_h.bought:
                verdict = ("holder acquired via protocol fills/claims (DCA, "
                           "limit orders, staking) — the holder IS the "
                           "acquirer; no feeding wallet")
            elif led_h.bought:
                verdict = "holder bought this itself"
            else:
                verdict = "no inbound transfers found"
            rows.append({"mint": m, "symbol": sym, "holder_bought": led_h.bought,
                         "holder_received": led_h.received,
                         "holder_protocol_in": led_h.protocol_in,
                         "feeder": None, "verdict": verdict})
            continue
        feeder, amount = max(feeders.items(), key=lambda kv: kv[1])
        led_f = reconcile_wallet(rpc, feeder, m, mi, symbols, "feeder")
        sent_to_holder = sum(-e.tokens for e in led_f.events
                             if e.kind == "transfer_out" and e.counterparty == holder)
        match = abs(sent_to_holder - amount) <= max(1e-6 * max(amount, 1), 10 ** -mi.decimals)
        multi_asset = len(led_f.outlay) > 4
        if multi_asset:
            verdict = (f"feeder trades in {len(led_f.outlay)} currencies — a "
                       "market-making/protocol entity, not a personal hot wallet")
        elif led_f.bought > 0 and led_h.bought == 0 and match:
            verdict = "CONFIRMED: feeder bought on market, holder only received"
        elif led_f.bought > 0 and led_h.bought == 0:
            verdict = ("feeder buys, holder only receives — but sent/received "
                       "amounts differ (other recipients or truncated history)")
        elif led_f.received > 0 and led_f.bought == 0:
            verdict = "feeder is itself a pass-through (received, did not buy) — trace one hop further"
        else:
            verdict = "not a clean hot→cold pattern: " + (
                "holder also buys" if led_h.bought else "feeder shows no purchases")
        rows.append({"mint": m, "symbol": sym, "holder_bought": led_h.bought,
                     "holder_received": led_h.received, "feeder": feeder,
                     "feeder_bought": led_f.bought, "feeder_buys": led_f.buys,
                     "feeder_outlay": led_f.outlay, "feeder_sold": led_f.sold,
                     "feeder_sent_to_holder": sent_to_holder,
                     "feeder_holds": led_f.balance_now,
                     "feeder_unfetchable": led_f.txs_unfetchable,
                     "feeder_notes": led_f.notes,
                     "holder_unfetchable": led_h.txs_unfetchable,
                     "verdict": verdict})
    # same-operator evidence: who gave the holder its first SOL?
    sol_funder = None
    try:
        page = rpc.signatures(holder, limit=1000)
        if page and len(page) < 1000:
            from .report import _inbound_funder
            tx = rpc.transaction(page[-1]["signature"])
            if tx:
                sol_funder = _inbound_funder(tx, holder)
    except Exception:
        pass
    feeders_seen = {r["feeder"] for r in rows if r.get("feeder")}
    return {"holder": holder, "rows": rows, "sol_funder": sol_funder,
            "skipped_mints": skipped,
            "sol_funder_is_feeder": bool(sol_funder and sol_funder in feeders_seen)}


def render_feeders(d: dict) -> str:
    out = [f"=== feeder check — is {d['holder']} a holding wallet fed by a trader? ==="]
    if d.get("skipped_mints"):
        out.append(f"[note] {d['skipped_mints']} further holding(s) not checked — "
                   "run with --mint <token> for a specific one")
    out.append("")
    for r in d["rows"]:
        out.append(f"{r['symbol']} {r['mint']}")
        out.append(f"    holder: bought {_fmt(r['holder_bought'])}, received {_fmt(r['holder_received'])}")
        if r.get("holder_protocol_in"):
            out.append(f"    holder received {_fmt(r['holder_protocol_in'])} via protocol fills/claims")
        if r.get("feeder"):
            out.append(f"    feeder {r['feeder']}: {r['feeder_buys']} purchases = "
                       f"{_fmt(r['feeder_bought'])} (outlay {_flows(r['feeder_outlay'])}), "
                       f"sold {_fmt(r['feeder_sold'])}, sent {_fmt(r['feeder_sent_to_holder'])} "
                       f"to holder, still holds {_fmt(r['feeder_holds'])}")
            if r.get("feeder_unfetchable") or r.get("holder_unfetchable"):
                out.append(f"    [note] unfetchable txs — feeder {r.get('feeder_unfetchable', 0)}, "
                           f"holder {r.get('holder_unfetchable', 0)}; verdict is on fetched data only")
                for n in r.get("feeder_notes") or []:
                    out.append(f"    [note] feeder: {n}")
        out.append(f"    → {r['verdict']}")
        out.append("")
    if d.get("sol_funder"):
        out.append(f"holder's first SOL came from {d['sol_funder']}"
                   + (" — the feeder itself: same operator, hot→cold wallet pair"
                      if d["sol_funder_is_feeder"] else " (not the feeder)"))
    else:
        out.append("holder's first SOL funder: not determined (deep history or no inbound found)")
    return "\n".join(out)


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
    if len(d) > 4:                 # a person pays in one or two currencies
        majors = {c: v for c, v in d.items() if c in ("SOL", "USDC", "USDT")}
        return (" + ".join(f"{_fmt(v)} {c}" for c, v in sorted(majors.items()))
                + (" + " if majors else "")
                + f"{len(d) - len(majors)} other tokens (multi-asset entity)")
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
        out.append(f"    wallet transfers: +{_fmt(l['received'])} in, -{_fmt(l['sent'])} out {sym}")
    if l.get("protocol_in") or l.get("protocol_out"):
        out.append(f"    protocol fills/claims: +{_fmt(l.get('protocol_in', 0))} in, "
                   f"escrows/deposits: -{_fmt(l.get('protocol_out', 0))} out {sym} "
                   "(order fills — holder is the acquirer)")
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
                                "transfer_in", "transfer_out",
                                "protocol_in", "protocol_out",
                                "lookalike_dust")]
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
    legend = {l["mint"]: l["symbol"] for l in d["ledgers"]}
    legend.update({m: sym for m, sym in _sym_cache.items() if sym})
    if legend:
        body.append("mints referenced (full addresses for --mint):")
        for m, sym in sorted(legend.items(), key=lambda kv: str(kv[1])):
            body.append(f"  {sym:<12} {m}")
    return "\n".join(body)


def main() -> int:
    global VERBOSE, MAX_TXS, MAX_SIG_PAGES
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("address")
    ap.add_argument("--mint", help="when address is a wallet: the token to "
                                   "reconcile — full mint or its ticker (e.g. HOTBOT)")
    ap.add_argument("--wallets", type=int, default=DEFAULT_WALLETS)
    ap.add_argument("--max-txs", type=int, default=None,
                    help=f"transactions per wallet×mint (default {MAX_TXS}); "
                         "raise for a full-history run")
    ap.add_argument("--debug", action="store_true",
                    help="progress on stderr + RPC diagnostics for unfetchable txs")
    ap.add_argument("--probe-sig", help="diagnose one transaction signature and exit")
    ap.add_argument("--feeders", action="store_true",
                    help="wallet address: verify it is a holding wallet fed by "
                         "a trading wallet (who sent, did they buy, do amounts match)")
    a = ap.parse_args()
    VERBOSE = a.debug
    if a.max_txs:
        MAX_TXS = a.max_txs
        MAX_SIG_PAGES = max(MAX_SIG_PAGES, a.max_txs // 1000 + 1)
    if a.probe_sig:
        for k, v in SolRpc().probe_transaction(a.probe_sig).items():
            print(f"{k:<24} {v}")
        return 0
    if not b58.looks_like_address(a.address):
        print("not a base58 Solana address", file=sys.stderr)
        return 2
    try:
        if a.feeders:
            print(render_feeders(feeder_check(SolRpc(), a.address, a.mint)))
            return 0
        print(render_text(reconcile(SolRpc(), a.address, a.mint, a.wallets)))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
