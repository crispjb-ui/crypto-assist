# crypto-assist — project instructions

On-chain diligence tooling, EVM-first (primary target: Robinhood Chain; works on
any EVM chain via `.env`). Python 3.11+, stdlib + `requests` only — do not add
web3.py or other heavy deps without being asked.

## Layout

- `src/onchain/rpc.py` — JSON-RPC client (batching, chunked `eth_getLogs`,
  deploy-block binary search). All chain access goes through this.
- `src/onchain/explorer.py` — Etherscan/Blockscout-compatible API (funding
  traces, verified source). Optional: every caller must degrade gracefully
  when `EXPLORER_API_URL` is unset.
- `src/onchain/early_buyers.py` → `clusters.py` → `holders.py` → `score.py` —
  the detection pipeline; `report.py` and `scan.py` are the CLIs.
- `.claude/skills/` — user-facing workflows (`/diligence`, `/bundle-check`,
  `/scan-launches`).

## Rules

- Zero-hallucination posture in all user-facing output: report only what was
  measured; explicitly name what could not be verified. Never present a clean
  scan as a buy recommendation.
- Chain specifics (RPC URL, explorer, chain id, DEX addresses) come from `.env`
  only — never hardcode a chain.
- Event topics and function selectors are hardcoded constants with their
  signatures in comments; verify the keccak hash before adding a new one.
- RPC spend is a real cost: respect `MAX_LOG_BLOCK_RANGE` /
  `MAX_EARLY_BUYERS` caps and batch calls.
- The remote Claude environment may block crypto API domains (proxy 403);
  live-network testing happens on the user's machine.
