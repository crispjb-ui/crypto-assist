---
name: bundle-check
description: Fast check whether a token launch was bundled/sniped by a coordinated wallet cluster. Use when the user asks "was this bundled", "check the snipers", or wants the launch-window analysis without the full report.
---

# Bundle check

Argument: a token address, optionally a pair address.

1. Run: `python -m src.onchain.report <TOKEN> [--pair <PAIR>] --bundle-only --json`
2. Report, in order:
   - Buys in the pair-creation block (≥3 = bundled with high confidence).
   - Distinct wallets within 2 blocks of creation.
   - Funding clusters: which single wallet funded multiple snipers. Quote the
     funder address so the user can look it up (it may be a CEX hot wallet —
     say so as a possibility, do not assert either way).
   - Offload status: how many snipers retain <10% of their buy. If most have
     offloaded, say plainly that early supply has already been distributed and
     current buyers are likely exit liquidity.
3. The offload *destination* (the "larger cluster" pattern) is not traced by the
   tooling yet — if the user wants it, trace the token Transfer logs out of the
   top sniper wallets manually with `eth_getLogs` and report common recipients.
