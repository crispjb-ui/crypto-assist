"""First funding source of an EVM wallet — the start of its lineage.

    CRYPTO_ASSIST_ENV=.env.bsc python -m src.onchain.funder 0xWALLET [0x...]

Uses the configured chain profile's explorer (EXPLORER_API_URL). Prints the
earliest inbound native transfer: sender, whether it arrived through a
contract (disperser/bridge/router), the transaction, and the sender's own
first funder one hop up. Says 'not traceable' rather than guessing when the
explorer has no record.
"""
from __future__ import annotations

import sys

from . import config, explorer


def describe(address: str, hops: int = 2) -> list[str]:
    out = []
    cur = address.lower()
    for hop in range(hops):
        ev = explorer.funding_event(cur)
        if not ev:
            out.append(f"  {cur}: first inbound native transfer not found "
                       f"({explorer.LAST_ERROR or 'no record / bridged-in via token transfer only'})")
            break
        src = ev.get("from") or "?"
        out.append(f"  {cur}: first funded by {src}"
                   + (" via contract (disperser/bridge/router — the human origin "
                      "is the tx sender, resolve on the explorer)" if ev.get("internal") else "")
                   + f"  tx {ev.get('hash')}  block {ev.get('block')}")
        if not src.startswith("0x") or src == cur:
            break
        cur = src
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    if not config.PROFILE_FOUND:
        print(f"profile file {config.PROFILE_NAME} not found in the repo root — "
              f"copy {config.PROFILE_NAME}.example to {config.PROFILE_NAME} and "
              "fill EXPLORER_API_KEY (BscScan / Etherscan key for BSC)",
              file=sys.stderr)
        return 2
    if not config.EXPLORER_API_URL:
        print(f"EXPLORER_API_URL is not set in {config.PROFILE_NAME} — funding "
              "traces need the chain's explorer", file=sys.stderr)
        return 2
    if not config.EXPLORER_API_KEY:
        print(f"EXPLORER_API_KEY is empty in {config.PROFILE_NAME} — Etherscan-"
              "family explorers refuse keyless requests", file=sys.stderr)
        return 2
    print(f"chain profile {config.CHAIN_KEY} — explorer {config.EXPLORER_API_URL}")
    for a in sys.argv[1:]:
        if not (a.startswith("0x") and len(a) == 42):
            print(f"skipping {a}: not a 0x address")
            continue
        print(f"\n{a}")
        for line in describe(a):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
