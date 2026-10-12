"""Cross-chain link finder — did a Solana wallet bridge to an EVM address?

A Solana wallet and an EVM wallet share nothing on-chain unless value moved
between them through a bridge. Bridge messages on Solana carry the EVM
destination, so scanning a wallet's history for bridge-program transactions
and extracting 20-byte addresses from their instruction data is the one
provable link. Everything else (same CEX, similar behavior) is not proof.

    python -m src.solana.bridges                       # watched wallets
    python -m src.solana.bridges <wallet> [...]
    python -m src.solana.bridges --evm 0xabc...,0xdef...   # match candidates

Program ids below are labeled only on exact match; '?' = inferred from the
program's vanity prefix, unverified. Unknown programs on a transaction that
carries an EVM-shaped word are printed raw so they can be identified.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import b58
from .rpc import SolRpc
from .watch import watched

BRIDGE_PROGRAMS = {
    "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth": "Wormhole core",
    "wormDTUJ6AWPNvk59vGQbDvGJmqbDTdgWgAqcLBCgUb": "Wormhole token bridge",
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
    raw instruction data — how EVM recipients travel in bridge messages."""
    try:
        raw = b58.decode(data_b58)
    except ValueError:
        return []
    out = []
    for i in range(0, len(raw) - 31):
        w = raw[i:i + 32]
        if w[:12] == b"\x00" * 12 and any(w[12:]) and w[12:18] != b"\x00" * 6:
            out.append("0x" + w[12:].hex())
    return sorted(set(out))


def scan_wallet(rpc: SolRpc, wallet: str, label: str = "") -> list[dict]:
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
            bridges = [BRIDGE_PROGRAMS[p] for p in progs if p in BRIDGE_PROGRAMS]
            if bridges or words:
                hits.append({"wallet": wallet, "label": label,
                             "sig": sg["signature"], "ts": sg.get("blockTime") or 0,
                             "bridges": bridges, "evm_candidates": sorted(words),
                             "unknown_programs": sorted(p for p in progs
                                                        if p not in BRIDGE_PROGRAMS
                                                        and p not in _SKIP)[:4]})
    return hits, complete, len(sigs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("wallets", nargs="*")
    ap.add_argument("--evm", help="comma-separated 0x addresses to match against")
    a = ap.parse_args()
    targets = [(w[:6], w) for w in a.wallets if b58.looks_like_address(w)] or watched()
    if not targets:
        print("no wallets: pass addresses or set SOLANA_WATCH_WALLETS", file=sys.stderr)
        return 2
    want = {x.strip().lower() for x in (a.evm or "").split(",") if x.strip()}
    rpc = SolRpc()
    import time
    any_hit = False
    for label, w in targets:
        hits, complete, n = scan_wallet(rpc, w, label)
        print(f"=== {label} {w}: {n} transactions scanned"
              + ("" if complete else f" (TRUNCATED at {MAX_PAGES * 1000} — raise SOLANA_BRIDGE_PAGES)") + " ===")
        if not hits:
            print("  no bridge-program transactions and no EVM-shaped recipient words found")
            continue
        any_hit = True
        for h in hits:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(h["ts"])) if h["ts"] else "?"
            print(f"  {when}  {h['sig'][:20]}…")
            if h["bridges"]:
                print(f"      bridge: {', '.join(h['bridges'])}")
            for c in h["evm_candidates"]:
                mark = "  <== MATCHES YOUR LIST" if c.lower() in want else ""
                print(f"      EVM-shaped word: {c}{mark}")
            if h["unknown_programs"] and not h["bridges"]:
                print(f"      programs: {', '.join(h['unknown_programs'])}")
    if want and not any_hit:
        print("\nno candidate EVM addresses found in these wallets' histories — "
              "no provable bridge link to the supplied list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
