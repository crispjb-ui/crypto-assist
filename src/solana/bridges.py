"""Cross-chain link finder — did a Solana wallet bridge to an EVM address?

A Solana wallet and an EVM wallet share nothing on-chain unless value moved
between them through a bridge. Bridge messages on Solana carry the EVM
destination, so scanning a wallet's history for bridge-program transactions
and extracting 20-byte addresses from their instruction data is the one
provable link. Everything else (same CEX, similar behavior) is not proof.

    python -m src.solana.bridges                       # watched wallets
    python -m src.solana.bridges <wallet> [...]
    python -m src.solana.bridges --evm 0xabc...,0xdef...   # match candidates
    python -m src.solana.bridges --all                 # also non-bridge words

By default a transaction is reported only when a known bridge program is
present or one of its EVM-shaped words matches --evm. Swaps and DCA fills
carry plenty of 32-byte words that happen to look like addresses; --all
prints those too (raw programs shown so a new bridge can be identified).

Program ids below are labeled only on exact match; '?' = inferred from the
program's vanity prefix, unverified.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import b58
from .rpc import SolRpc
from .watch import watched

BRIDGE_PROGRAMS = {
    "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth": "Wormhole core",
    "wormDTUJ6AWPNvk59vGQbDvGJmqbDTdgWgAqcLBCgUb": "Wormhole token bridge",
    # Circle CCTP (USDC burn-and-mint). v2 ids measured on a watched wallet
    # 2026-10-05/06; role split (message transmitter / token messenger) not
    # verified here, hence '?'.
    "CCTPV2Sm4AdWt5296sk4P66VBZ7bEhcARwFaaS9YPbeC": "Circle CCTP v2?",
    "CCTPV2vPZJS2u2BBsUoscuikbYjnpFmbFsvVuJdgUMQe": "Circle CCTP v2?",
    "CCTPiPYPc6AsJuwueEnWgSgucamXDZwBdQcMzXD7VqXb": "Circle CCTP v1?",
    "CCTPmbSD7gX1bxKPAmg77w8oFzNFpaQiQUWD43TKaecd": "Circle CCTP v1?",
    "DEbrdGj3HsRsAzx6uH4MKyREKxVAfBydijLUF3ygsFfh": "deBridge?",
    "FC4eXxkyrMPTjiYUpp4EAnkmwMbQyZ6NDCh1kfLn6vsf": "Mayan?",
    "BrdgN2RPzEMWF96ZbnnJaUtQDQx7VRXYaHHbYCBvceWB": "Allbridge?",
}
_SKIP = {"11111111111111111111111111111111",
         "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
         "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
         "ComputeBudget111111111111111111111111111111",
         "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
         "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"}
MAX_PAGES = int(os.environ.get("SOLANA_BRIDGE_PAGES", "5"))


def evm_words(data_b58: str) -> list[str]:
    """0x addresses encoded as 32-byte words (12 zero bytes + 20 bytes) in
    raw instruction data — how EVM recipients travel in bridge messages.
    Sliding window: CCTP's recipient word sits at a non-aligned offset."""
    try:
        raw = b58.decode(data_b58)
    except ValueError:
        return []
    out = []
    for i in range(0, len(raw) - 31):
        w = raw[i:i + 32]
        # first address byte must be non-zero: a zero there is the sliding
        # window straddling the padding, which otherwise yields shifted
        # look-alikes of the real word (addresses starting 0x00.. are missed —
        # 1 in 256, documented trade-off)
        if w[:12] == b"\x00" * 12 and w[12] != 0 and w[12:18] != b"\x00" * 6:
            out.append("0x" + w[12:].hex())
    return sorted(set(out))


# Mint -> symbol for the delta readout; anything else prints as mint[:6].
_KNOWN_MINTS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}


def wallet_deltas(tx: dict, wallet: str) -> list[str]:
    """Token balance changes of `wallet` in this transaction, from meta
    pre/postTokenBalances — the amount actually burned for a bridge."""
    meta = tx.get("meta") or {}
    pre, post = {}, {}
    for src, bucket in ((meta.get("preTokenBalances") or [], pre),
                        (meta.get("postTokenBalances") or [], post)):
        for b in src:
            if b.get("owner") != wallet:
                continue
            ui = b.get("uiTokenAmount") or {}
            bucket[b["mint"]] = (bucket.get(b["mint"], (0, 0))[0] + int(ui.get("amount") or 0),
                                 int(ui.get("decimals") or 0))
    out = []
    for mint in sorted(set(pre) | set(post)):
        a0, d = pre.get(mint, (0, post.get(mint, (0, 0))[1]))
        a1, _ = post.get(mint, (0, d))
        if a1 != a0:
            out.append(f"{(a1 - a0) / 10 ** d:+,.{min(d, 6)}f} {_KNOWN_MINTS.get(mint, mint[:6])}")
    return out


def classify_tx(tx: dict) -> tuple[list[str], list[str], list[str]]:
    """(bridge labels, EVM-shaped words, unknown program ids) for one
    jsonParsed transaction, top-level and inner instructions."""
    msg = tx["transaction"]["message"]
    groups = [msg.get("instructions") or []]
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        groups.append(inner.get("instructions") or [])
    progs, words = set(), set()
    for instrs in groups:
        for ins in instrs:
            pid = ins.get("programId")
            if pid:
                progs.add(pid)
            if isinstance(ins.get("data"), str):
                words.update(evm_words(ins["data"]))
    bridges = sorted({BRIDGE_PROGRAMS[p] for p in progs if p in BRIDGE_PROGRAMS})
    unknown = sorted(p for p in progs if p not in BRIDGE_PROGRAMS and p not in _SKIP)
    return bridges, sorted(words), unknown


def scan_wallet(rpc: SolRpc, wallet: str, label: str = "") -> tuple[list[dict], bool, int]:
    sigs, complete = rpc.signatures_full(wallet, max_pages=MAX_PAGES)
    sigs = [s for s in sigs if not s.get("err")]
    hits = []
    for i in range(0, len(sigs), 20):
        chunk = sigs[i:i + 20]
        txs = rpc.transactions([s["signature"] for s in chunk])
        for sg, tx in zip(chunk, txs):
            if not tx:
                tx = rpc.transaction(sg["signature"])
            if not tx:
                continue
            bridges, words, unknown = classify_tx(tx)
            if bridges or words:
                hits.append({"wallet": wallet, "label": label,
                             "sig": sg["signature"], "ts": sg.get("blockTime") or 0,
                             "bridges": bridges, "evm_candidates": words,
                             "deltas": wallet_deltas(tx, wallet),
                             "unknown_programs": unknown[:4]})
    return hits, complete, len(sigs)


def select_hits(hits: list[dict], want: set[str], show_all: bool) -> list[dict]:
    """Default view: bridge-program transactions plus any transaction whose
    words match --evm. Everything else is swap/DCA noise unless --all."""
    if show_all:
        return hits
    return [h for h in hits
            if h["bridges"] or any(c.lower() in want for c in h["evm_candidates"])]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("wallets", nargs="*")
    ap.add_argument("--evm", help="comma-separated 0x addresses to match against")
    ap.add_argument("--all", action="store_true",
                    help="print every transaction carrying an EVM-shaped word, "
                         "not only bridge-program ones / --evm matches")
    a = ap.parse_args()
    targets = [(w[:6], w) for w in a.wallets if b58.looks_like_address(w)] or watched()
    if not targets:
        print("no wallets: pass addresses or set SOLANA_WATCH_WALLETS", file=sys.stderr)
        return 2
    want = {x.strip().lower() for x in (a.evm or "").split(",") if x.strip()}
    rpc = SolRpc()
    links: list[tuple[dict, str]] = []
    for label, w in targets:
        hits, complete, n = scan_wallet(rpc, w, label)
        shown = select_hits(hits, want, a.all)
        print(f"=== {label} {w}: {n} transactions scanned"
              + ("" if complete else f" (TRUNCATED at {MAX_PAGES * 1000} — raise SOLANA_BRIDGE_PAGES)") + " ===")
        if not shown:
            hidden = len(hits)
            print("  no bridge-program transactions"
                  + (f"; {hidden} swap/DCA transactions carry EVM-shaped words "
                     f"(none match --evm; --all prints them)" if hidden else
                     " and no EVM-shaped recipient words found"))
            continue
        for h in shown:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(h["ts"])) if h["ts"] else "?"
            print(f"  {when}  [{label}]  {h['sig'][:20]}…")
            if h["bridges"]:
                print(f"      bridge: {', '.join(h['bridges'])}")
            if h.get("deltas"):
                print(f"      wallet token change: {', '.join(h['deltas'])}")
            for c in h["evm_candidates"]:
                if c.lower() in want:
                    links.append((h, c))
                    print(f"      EVM-shaped word: {c}  <== MATCHES YOUR LIST")
                else:
                    print(f"      EVM-shaped word: {c}")
            if h["unknown_programs"] and not h["bridges"]:
                print(f"      programs: {', '.join(h['unknown_programs'])}")
    if want:
        print()
        if links:
            print("LINKS (Solana wallet -> EVM address found in a bridge-shaped word):")
            for h, c in links:
                when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(h["ts"])) if h["ts"] else "?"
                via = ", ".join(h["bridges"]) or "unlabeled program(s): " + ", ".join(h["unknown_programs"])
                amt = ("  " + ", ".join(h["deltas"])) if h.get("deltas") else ""
                print(f"  {h['label']} {h['wallet']}\n      -> {c}\n      {when} via {via}{amt}\n      sig {h['sig']}")
        else:
            print("no supplied EVM address appears in these wallets' histories — "
                  "no provable bridge link")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
