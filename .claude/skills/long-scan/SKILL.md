---
name: long-scan
description: Watch Long (long.xyz) launchpad launches on Robinhood Chain — stock-paired tokens with Uniswap v4 pools. Use when the user asks about Long launches, stock-paired tokens, or wants the Long launch feed.
---

# Long launch scan

1. Run: `python -m src.onchain.long --hours 6 --json` (adjust `--hours`).
2. For each launch report: token + symbol, the tokenized stock it trades
   against (`paired_symbol` — NVDA, TSLA, ...), early-buy pressure
   (`early_buys`/`early_buyers`, `buys_in_launch_block`), and
   `offloaded_top` (of the 10 largest early buyers, how many now hold <10%
   of what they bought — the offload signal).
3. Long has NO snipe tax, so unlike Pons there is no declared-exemption
   list: bundle detection rests on same-block buys, funding clusters, and
   offload. For a full check:
   `python -m src.onchain.report <token> --pair 0x8366a39cc670b4001a1121b8f6a443a643e40951 --creation-block <blk>`
   (the pair is the shared Uniswap v4 PoolManager; the creation block comes
   from the feed line).
4. Caveats to state plainly: Long's contracts are unpublished — the factory
   and events here were derived empirically from the flagship AI/NVDA launch
   tx; launches through a different Long entrypoint would be missed. The
   stock-paired design means price also moves with the underlying equity —
   a drawdown is not automatically a rug; check liquidity, not just price.
