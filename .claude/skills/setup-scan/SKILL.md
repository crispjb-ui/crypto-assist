---
name: setup-scan
description: Self-identify accumulation setups on the configured chain — rally, deep flush (≥60%), early recovery on a diligence-clean token. Use when the user asks for setups, dip/accumulation plays, retraced tokens popping back, or the rally-flush-breakout pattern.
---

# Accumulation-setup scan

1. Run: `python -m src.onchain.setups --json` (network-wide, self-identifying —
   no CA input needed). For one token: `--token 0xCA`.
2. The measured structure per candidate: `drawdown_pct` (peak-to-trough flush),
   `recovery_pct` (bounce off the low), `current_vs_ath` (how much room
   remains), `days_since_ath`, liquidity, and `diligence_score` from the
   ledger (tokens scoring >2 are excluded automatically).
3. Present the qualifiers ranked, then for any the user cares about, complete
   the missing half of the thesis: run `/diligence` and read holder
   concentration — the pattern's premise is "supply migrated to holders who
   won't sell", and only the holder check tests that. Structure alone is a
   chart shape; sellers may simply be waiting higher.
4. State plainly: thresholds are hypotheses, not calibrated facts, until
   enough setups have labeled outcomes in the ledger; GeckoTerminal history
   is the source and may lag on brand-new pools; and no output is a buy
   signal — it is a research queue for the pattern the user hunts.
