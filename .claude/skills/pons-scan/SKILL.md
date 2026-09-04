---
name: pons-scan
description: Watch Pons launchpad launches on Robinhood Chain and expose declared sniper bundles (snipe-tax exemption lists). Use when the user asks about Pons launches, bundled launches on Robinhood Chain, or wants the launch feed.
---

# Pons launch scan

1. Run: `python -m src.onchain.pons --hours 6 --json` (adjust `--hours`).
2. For each launch, report:
   - **Declared exemptions**: Pons V2 launches carry a ~99% snipe tax for the
     first seconds; the launch call may declare up to 32 tax-exempt wallets.
     A non-trivial exemption list IS the operator's bundle, self-published.
     Quote the count and the addresses for anything the user wants to track.
   - **Tax-free snipe buys** vs **taxed outside buys** in the launch window:
     tax-free buys from non-deployer wallets are the bundle executing; taxed
     buys are outsiders chasing.
   - **Graduated** status (curve completed into a locked Uniswap V4 pool —
     only ~1% of Pons launches graduate).
   - `exemption_source` tells you how the list was obtained: "exact" (decoded
     from a known factory selector), "heuristic" (address[] recovered from
     unrecognized router calldata — present it as *candidate* bundle wallets,
     cross-check against which wallets actually bought tax-free), or "opaque"
     (nothing decodable — say "exemption list not decodable", never "no
     exemptions"). The scan prints any unrecognized entrypoint contract +
     selector to stderr; relay that line to the user so exact support can be
     added.
3. Cross-reference interesting tokens with `/bundle-check` or `/diligence`
   (pass the curve address as `--pair` pre-graduation).
4. A launch with a large declared bundle + fast offload after graduation is
   the farm pattern; a launch with a small/no bundle, taxed organic demand,
   and holder growth after graduation is the early-project profile the user
   is hunting.
