# Behavioral examples

Illustrative shapes of correct handling — NOT live findings. Do not present
these as real results for any token.

## 1. Locked canonical LP, removable side liquidity
Canonical full-range position sits in a locker with no live withdrawal path
(rate: canonical LP-principal custody = strong). But a small v3 side position
is EOA-owned and decrease-liquidity-able (rate: side-pool removal risk =
present). Verdict must NOT read "liquidity locked" — it reads "canonical
locked; ~X% of active liquidity is EOA-removable at the pin."

## 2. Fixed supply, severe holder-sized exit degradation
No mint/owner/proxy in runtime (token controls = strong). But pinned quotes
show a holder-sized sell degrades per-token output 40–60% vs a 1k baseline
(sellability/exit depth = weak). "Immutable supply" and "thin exit" are both
true; report them separately, never net them.

## 3. Upgradeable reward layer around an immutable token
The ERC-20 is immutable, but rewards/backing route through an upgradeable
proxy whose admin can change distribution (admin/reward custody = weak;
token controls = strong). The immutable token does not make the *system*
safe.

## 4. Launch wallets selling and rebuying for new recipients
Opening cohort sells into the curve and rebuys to fresh recipients in the same
receipts (launch integrity = critical). Describe as market-mediated
redistribution across a pre-funded cohort; prove sales from curve mechanics,
not router transfers; do NOT assert one human owner. "A wallet reached zero"
is not a cash-out.

## 5. Vault holding synthetic claims, no proven underlying exit
A vault shows a large balance, but it holds synthetic reward claims with no
demonstrated redemption route to an underlying asset (redemption rights =
unproven). "Backed" is not established; state the missing exit.

## 6. RPC failure stays a coverage limitation
Historical state needed for a concentration claim is pruned on the available
RPC. That check is `coverage-limited`, the claim is `unknown` — never a pass.
The verdict names the gap and how it bounds the conclusion.

## Example invocations
- Focused: "Did token X's fee vault balance come from LP fees? Chain <id>,
  address <0x…>." → freeze packet, run surface F (+ G if a vault), reconcile,
  answer with bounded delta.
- Broad: "Full diligence on <0x…> on chain <id>, rug-resistance decision." →
  screen A–H, deepen on triggers, layered verdict, build manifest, run
  validator.
