# Target packet (freeze before investigating)

Assemble this once, freeze it, and bind every query, artifact, and conclusion
to it. If parallel agents are used, give each the same frozen packet and a
bounded lane; require evidence rows, findings, and unresolved questions back —
not free-form reports.

## Fields

```
requested_chain_id:        # what the user asked for
observed_chain_id:         # eth_chainId from the RPC actually used
requested_address:         # exact, checksum or lowercased consistently
observed_address:          # confirmed to have code / be the intended target
name / symbol / decimals / total_supply:
                           # from the target contract; if missing or nonstandard,
                           # record as UNRESOLVED (never guess, never borrow from
                           # a same-symbol token)
block_pin:
  number:
  hash:
  utc_timestamp:
  captured_header:         # raw block header fields used for the pin
runtime_code_hash:         # keccak256 of eth_getCode at the pin
proxy:
  is_proxy:                # EIP-1967 / EIP-1822 / beacon / minimal-proxy, etc.
  implementation:          # resolved impl address + its own code hash
  upgrade_authority:       # who can upgrade, and how (role/timelock/multisig)
deploy_or_launch_tx:       # hash + block, when available
candidate_pools:           # each by exact address or complete pool key
related_contracts:         # lockers, hooks, position managers, routers, vaults
decision_question:         # the user's actual question
scope:                     # focused | broad | formal report
materiality_rules:         # monetary thresholds (do NOT apply to authority discovery)
known_limitations:         # pruning, missing explorer API, rate limits, etc.
additional_chain_pins:     # each extra chain gets its own block/hash/utc pin
```

## Cheap architecture pass

Before deep work, enumerate every contract or key that can: change balances,
restrict transfers, remove principal, upgrade behavior, collect fees, allocate
rewards, or enforce claimed utility. That list is the scope map; prioritize
checks that can change the conclusion.

## Efficiency

- Batch independent reads.
- Cache responses keyed by (chain, address, block, query).
- Deduplicate identical runtimes by code hash — analyze a shared implementation
  once.
- A question about a fee-vault's origin does not require a full audit; expand
  only into necessary dependencies.
