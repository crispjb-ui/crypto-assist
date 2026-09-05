# Core risk surfaces (A–H)

Screen all in broad mode; in focused mode, only the asked surface and its
dependencies. Do NOT gate the *discovery* of mint/upgrade/seizure/transfer-
restriction/arbitrary-call/LP-removal authority behind monetary materiality —
those are decisive at any size.

## A. Token code and control
Inspect: minting, burning, rebasing, balance rewrites, seizure, pause,
blacklist/whitelist, taxes, fee exemptions, cooldowns, transaction/wallet
limits, trading gates, external calls, delegatecall, upgrade paths.
Resolve: current owners, roles, multisig thresholds, timelocks, and who can
change them. Distinguish what the *current* code permits from what an admin
could introduce by *replacing* it (upgradeable ⇒ assume the impl can change
unless upgrade authority is provably neutralized).
"No owner" / "renounced" are claims to verify against storage and the deployed
runtime, not system-wide conclusions.

## B. Liquidity custody
Identify each material pool by exact address or complete pool key.
- v3: position manager, NFT/position id, tick range, liquidity, owner,
  approvals, operators, locker authority.
- v4: currency0/currency1 ordering, fee, tick spacing, hook, derived PoolId;
  never read the singleton PoolManager balance as one pool's reserves.
Inspect withdrawal, decrease-liquidity, burn-position, rescue, arbitrary-call,
approval, upgrade, and transfer paths. Separate canonical liquidity from side
pools; declare discovery sources, searched ranges, coverage limits.
A locked canonical position does not prove all liquidity is locked, that price
is supported, or that liquidity stays in range.

## C. Sellability and executable depth
Find a successful historical sell when available; obtain current pinned
read-only quotes at a small size and at holder-sized amounts. Report route,
input, quote asset, expected output, fees, per-unit degradation, failure
reasons. Distinguish price impact, slippage tolerance, gas, spot price, and
executable depth.
Historical execution proves execution at that historical state; quotes/previews
do not prove a realized exit. A decisive simulated sale/redemption requires a
successful receipt PLUS the intended underlying-asset balance delta, route and
costs explained — a success flag or emitted event alone is insufficient.

## D. Supply and concentration
Reconcile supply and material balances with methods appropriate to the token's
accounting. Classify separately: pool custody, protocol custody, lockers,
treasuries, burn addresses, creator allocations, investor-like balances. State
concentration denominators and exclusions. Keep raw assets, rebasing units,
synthetic claims, bond/NFT shares, LP shares, custody, total supply, and
circulating float distinct. Use full Transfer replay when discrepancies or
historical questions justify it; account for tokens whose balances change
without ordinary Transfer events.

## E. Launch integrity
Decode launch parameters, allocations, fee exemptions, direct-buy recipients,
funding, deterministic addresses, deployment sequence, early transfers/sales.
Distinguish automatic launch-platform behavior from manually supplied
exceptions; verify the exact factory version and deployed behavior.
Define any wallet cohort before measuring it: inclusion rule, time bounds,
sources, exclusions, coverage. Track initial allocation → transfers → sales →
rebuys → downstream inventory → proceeds → fees → retained assets. A wallet
reaching zero does not prove a cash-out. Prove sales from successful execution
and pool/curve mechanics; router transfers alone are insufficient.

## F. Fees, treasury, and proceeds
Map fee basis, denomination, splits, escrow, claim authority, recipients,
configurable routes, subsequent use. Separate a percentage of gross trade value
from a percentage of a fee bucket; separate current config from historically
realized rates. Reconcile each asset:
`opening + inflows + explained adjustments = outflows + closing + bounded
unexplained delta`. Account for wraps, burns, bridge legs, gas, reverted txs
correctly; do not double-count transformations. For bridges, match source
execution, identifiers, destination chain, recipient, delivered amount, and
destination evidence. Stop exact attribution at commingling — an exchange
deposit does not prove a sale, fiat withdrawal, or final beneficiary.

## G. Rewards, vaults, backing, redemption
Separate inventory from holder liabilities, and promises from enforceable
rights. Identify who can claim, actual asset received, conversion units, fees,
timing, caps, approvals, admin dependencies, exit route. A vault balance is not
necessarily available backing; a synthetic reward is not necessarily
redeemable. For "did these holdings come from LP fees?", reconcile balances and
transfers to receipt-level fee collections and forwarding, distinguishing vault
holdings, pool inventory, and unclaimed fees. For distributions, test
conservation, entitlement rules, cumulative caps, duplicate payments, unpaid
amounts, retained inventory, processing liveness. Do not assume distributed <
purchased if prefunding, donations, carryover, or minting exist.

## H. Utility, dependencies, and development
Determine whether advertised utility is live, token-linked, and enforceable.
Inspect external assets, oracles, bridges, APIs, keepers, lending systems,
collateral, redemption dependencies. Assess source correspondence, reproducible
builds, tests, audit scope, release controls, governance, disclosure accuracy,
actual operating behavior. Polished marketing, copied templates, and awkward
code establish neither safety nor fraud nor AI authorship.
