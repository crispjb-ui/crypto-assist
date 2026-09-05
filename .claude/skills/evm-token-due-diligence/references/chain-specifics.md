# Chain and platform specifics

Load only when the target uses these. Verify every address/selector against the
deployed runtime on the pinned chain — do not trust these notes as current
truth without confirmation.

## Uniswap v3
- Pool identity: (token0, token1, fee). Resolve the pool from the factory's
  `getPool(token0, token1, fee)` and confirm code at the address.
- Positions are NFTs in the NonfungiblePositionManager. Resolve position id →
  owner, tick range, liquidity, tokensOwed, and operator approvals.
- "Locked" means the position NFT is held by a locker/timelock with no live
  withdrawal path for the beneficiary. Verify the locker's own code and
  authority, decrease-liquidity/collect/rescue/arbitrary-call routes, and NFT
  transfer/approval state. A locked canonical position says nothing about side
  pools.

## Uniswap v4
- Liquidity lives in a single **PoolManager** singleton. A pool is identified by
  a PoolKey `(currency0, currency1, fee, tickSpacing, hooks)` with
  `currency0 < currency1`; PoolId = keccak256 of the ABI-encoded key.
- NEVER treat the PoolManager's ERC-20 balance as one pool's reserves — it
  holds every pool's tokens commingled. Read per-pool state via the id.
- The **hook** can implement fees, gating, and custom logic; resolve the hook
  contract, its authority, and upgrade path. A malicious/upgradeable hook is a
  control surface.
- Record currency ordering, fee, tick spacing, hook, and derived PoolId in the
  target packet.

## Pons launch platform
- Two generations (verify factory addresses against the deployed chain; do not
  hardcode from memory):
  - V1: CREATE2 factory mints a fixed-supply ERC-20, opens a one-sided Uniswap
    V3 position, locks the NFT; optional atomic dev buy.
  - V2: full supply mints to a constant-product bonding curve trading in the
    future quote asset, then graduates into a locked full-range Uniswap V4 pool
    governed by a shared hook, with a snipe tax and creator fee.
- **Snipe-tax exemptions**: a V2 launch call may declare wallets exempt from the
  decaying anti-snipe surcharge. That declared list is the operator's opening
  cohort — but decode it from the *deployed* factory ABI (which can differ from
  published source), and corroborate against wallets that actually bought
  tax-free in the launch window. Execution truth (who bought at tax==0) is
  selector-independent and authoritative; calldata decoding is corroboration.
- **Curve buy recipient**: inspect the direct curve-buy *recipient* and the tax
  actually applied, rather than assuming the transaction sender received the
  benefit.
- Graduation moves liquidity into a v4 pool — apply the v4 rules above.

## Robinhood Chain
- EVM L2 (Arbitrum Orbit stack), chain id 4663. Blocks are fast (~100–150 ms),
  so time-based analysis windows must be derived from a measured block rate, not
  a fixed block count.
- Confirm chain id from RPC before use; pin block/hash/UTC as on any chain.
- Explorer is Blockscout-family (Etherscan-compatible API). Internal transfers
  (`txlistinternal`) matter: launch cohorts are often funded through a
  disperser/airdrop contract, invisible to external `txlist` alone.
