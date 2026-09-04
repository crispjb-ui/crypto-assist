# crypto-assist

On-chain diligence infrastructure for Claude Code, EVM-first. Purpose: identify bundled launches, sniper/offload wallet clusters, and farm projects quickly and consistently — and surface early projects worth deeper research.

Primary target: **Robinhood Chain** (EVM, Arbitrum Orbit stack). The tooling is chain-agnostic EVM — everything chain-specific comes from `.env`, so it runs unchanged on Arbitrum, Base, Ethereum, or any other EVM chain. Note: Robinhood Chain's public RPC endpoints / chain ID / explorer are not hardcoded here because they could not be verified at build time — you supply them in `.env` (see below).

## What this detects

The farm pattern from the source thread, decomposed into measurable on-chain signals:

| Signal | What it means | Module |
|---|---|---|
| Same-block / first-blocks buys | Wallets buying in the pair-creation block or immediately after = snipers, likely a bundled launch | `early_buyers.py` |
| Common funding source | Multiple early buyers funded by the same wallet = one operator, not organic demand | `clusters.py` |
| Fresh wallets sniping | Snipe wallets with near-zero nonce at launch = purpose-created for this launch | `clusters.py` |
| Fast offload | Sniper wallets now holding ~0% of what they bought = supply already dumped or moved to a second cluster | `clusters.py` |
| Holder concentration | Top-N holders control a large share of supply | `holders.py` |
| Contract red flags | Owner not renounced, unverified source, proxy (upgradeable), mint/blacklist/tax hooks in source | `erc20.py` |
| Paid promotion | Token buying DexScreener boosts while the above flags are up = farm profile | `scan.py` |

`report.py` combines these into a red-flag score. `scan.py` pulls DexScreener discovery feeds and filters young pairs so you screen many tokens cheaply before spending RPC calls on deep checks.

## What you need to establish this

1. **A paid EVM RPC for your target chain** — deep checks page event logs and historical state; free public endpoints rate-limit immediately. Any provider that serves the chain works (dRPC, QuickNode, Alchemy, or the chain's own endpoint). Historical `eth_getCode`/`eth_getLogs` depth matters more than raw speed. Set `EVM_RPC_URL`.
2. **An Etherscan/Blockscout-compatible explorer API** for the chain (`EXPLORER_API_URL`) — this is what enables funding-source tracing ("who funded the sniper wallets") and contract-source checks. Arbitrum-Orbit chains typically ship a Blockscout instance; Blockscout exposes the Etherscan-compatible `?module=account&action=txlist` API this repo uses. Without it, funding-source checks degrade gracefully to the nonce heuristic.
3. **Free market data** — DexScreener REST API (no key) for pair discovery, liquidity, volume, age, and boost feeds — *if* it indexes your chain (`DEXSCREENER_CHAIN_ID`). If not, pass pair addresses manually with `--pair`.
4. **Claude Code** — `.claude/skills/` here give any session the workflows: `/diligence`, `/bundle-check`, `/scan-launches`.
5. **Optional enrichment** (extension points, not wired in): Bubblemaps (visual cluster confirmation), GoPlus token-security API, honeypot simulation via `eth_call` state overrides, X/Twitter API for astroturf detection.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in EVM_RPC_URL; EXPLORER_API_URL strongly recommended
```

## Usage

```bash
# Full diligence report on a token
python -m src.onchain.report 0xTOKEN

# If DexScreener doesn't index the chain, name the pair/pool yourself
python -m src.onchain.report 0xTOKEN --pair 0xPAIR

# Scan DexScreener discovery feeds for young pairs on your chain
python -m src.onchain.scan --max-age-hours 24 --min-liquidity-usd 20000
```

Or in Claude Code: `/diligence 0xTOKEN`, `/scan-launches`.

## Interpreting results

- **Red-flag score ≥ 5**: treat as a farm until proven otherwise.
- **Snipers retain <10% of what they bought**: the offload already happened; late buyers are exit liquidity.
- **No flags ≠ good project**: this removes obviously rigged launches. Positive selection (team, product, sustained organic holder growth) is separate judgment this repo assists — re-run `/diligence` after 24–72h and compare holder-distribution drift.

## Limits (read this)

- Heuristics, not proof. A common funder can be a CEX hot wallet; the tooling reports the funder address and lets you judge. One funding hop is traced — deeper graphs are an extension point.
- Log paging and history scans are capped (configurable) to control RPC cost; the target use case is *young* tokens where full reconstruction is cheap.
- Bridged-in funding (cluster funded from an L1/another chain) is invisible to single-chain tracing — a cluster of fresh wallets with *no* on-chain funder is itself a flag.
- This remote Claude environment's network policy may block crypto API domains. Run locally, or allowlist your RPC host, explorer host, and `api.dexscreener.com`.

Nothing here is financial advice; it is plumbing for your own judgment.
