---
name: scan-launches
description: Scan for young token launches on the configured chain and pre-rank candidates for diligence. Use when the user asks to find new projects, new pairs, or early opportunities.
---

# Launch scan

1. Run: `python -m src.onchain.scan --max-age-hours 24 --min-liquidity-usd 10000`
   Adjust filters if the user specifies (age, liquidity, volume, `--chain`).
2. If it exits saying the chain is not indexed by DexScreener, tell the user the
   alternative: watch the chain's DEX factory contract for pool-creation events
   (needs the factory address) — offer to build that watcher.
3. Present candidates as a table: symbol, address, age, liquidity, 24h volume,
   and flag any token marked `paying_for_boosts` (paid promotion on a brand-new
   token is a farm signal, per the source thesis).
4. Offer to run `/diligence` on the top candidates. Do not editorialize about
   which will "pump" — rank only by the measured data.
