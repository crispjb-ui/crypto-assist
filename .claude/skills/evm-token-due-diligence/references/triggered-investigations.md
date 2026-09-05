# Triggered deep investigations

Each track states: **trigger**, **minimum evidence**, **stopping condition**,
and **what remains unknown** if it cannot be completed. Enter a track only when
its trigger fires; a triggered track that cannot complete leaves an explicit
unknown, never a pass.

## 1. Full holder / Transfer replay
- Trigger: balance/supply discrepancy, concentration question, or historical
  ownership claim that snapshot balances cannot answer.
- Min evidence: Transfer logs from deploy to pin, plus handling for balance
  changes without ordinary Transfer events (rebase, mint/burn hooks).
- Stop: reconciled balances within a stated tolerance.
- Unknown if incomplete: pre-gap distribution; note the missing block range.

## 2. Complete pool / position history
- Trigger: liquidity-custody or price-support question beyond current state.
- Min evidence: mint/burn/modify-liquidity events, position ownership changes,
  locker interactions across the pool's life.
- Stop: every material position accounted for at the pin.
- Unknown if incomplete: side pools outside the searched range — declare it.

## 3. Launch-cohort accounting
- Trigger: bundle/insider or preferential-allocation question.
- Min evidence: cohort definition (inclusion rule, time bounds, sources,
  exclusions), then allocation → transfer → sale → rebuy → downstream inventory
  → proceeds → retained, with sales proven from execution/curve mechanics.
- Stop: cohort inventory reconciled to zero-or-held with proceeds bounded.
- Unknown if incomplete: ultimate beneficiary (commingling), off-chain identity.

## 4. Fee-wallet / cross-chain proceeds reconciliation
- Trigger: "where did fees/treasury go" or a proceeds claim.
- Min evidence: per-asset ledger (opening + inflows + adjustments = outflows +
  closing + bounded delta); bridge legs matched source↔destination.
- Stop: each asset reconciled or delta explicitly bounded.
- Unknown if incomplete: attribution past an exchange deposit or commingling.

## 5. Proxy authority / critical bytecode reconstruction
- Trigger: upgradeable target, unverified runtime, or source-mismatch.
- Min evidence: resolve proxy/impl/beacon and upgrade authority; escalate
  gradually — runtime & selectors → verified predecessors & compiler metadata →
  storage & historical calls → deeper reconstruction/simulation.
- Stop: the specific authority/behavior question is answered.
- Unknown if incomplete: exact impl semantics — decompiler output is NOT
  verified source; claim executable equivalence only after compile + byte
  comparison closes meaningful differences.

## 6. Reward-epoch accounting / backlog modeling
- Trigger: distribution/backing claim material to the verdict.
- Min evidence: conservation, entitlement rules, cumulative caps, duplicate/
  unpaid checks, retained inventory, processing liveness.
- Stop: distribution invariants hold or a specific violation is shown.
- Unknown if incomplete: future solvency under unmodeled inflows.

## 7. External dependency / redemption analysis
- Trigger: advertised utility, backing, oracle, bridge, or lending dependency.
- Min evidence: is the dependency live, token-linked, enforceable; who controls
  it; actual redemption route and asset delivered.
- Stop: the right is shown enforceable or shown to be a promise.
- Unknown if incomplete: off-chain operator behavior.

## 8. Narrow operational attribution
- Trigger: a specific control/beneficiary question with authenticated
  project-control evidence.
- Min evidence: tie to project-control signatures/authority, not just shared
  funding/timing/routers.
- Stop: the narrow question is answered.
- Unknown if incomplete: human identity — keep attribution neutral and narrow.

Route protocol-wide invariant work or substantial exploit campaigns to a
separate audit workflow when available; this skill is token/economic-system
diligence, not a full exploit audit.
