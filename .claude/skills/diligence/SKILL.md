---
name: diligence
description: Full on-chain diligence on an EVM token — bundle detection, wallet clusters, holder concentration, contract red flags. Use when the user gives a token address and asks whether it's safe, legit, bundled, or worth investigating.
---

# Token diligence

Argument: a token address, optionally a pair address.

1. Run: `python -m src.onchain.report <TOKEN> [--pair <PAIR>] --json`
   (from the repo root; `.env` must have `EVM_RPC_URL`).
2. If it exits with "No pair found", ask the user for the pair/pool address or
   find it on the chain's explorer, then re-run with `--pair`.
3. Interpret the JSON for the user. Lead with the verdict line and score, then
   the flags in plain language. Always include:
   - Whether the launch was bundled (buys in creation block, funding clusters).
   - **Coordinated-lineage signals**: a common funder covering many early
     buyers, funding routed through a disperser/airdrop contract
     (`funder_via_contract` — batch-funded cohort, not organic arrivals), and
     same-tx sell→rebuy recycling to fresh wallets (`recycle_txs` —
     choreography that inflates apparent holder count without new capital).
     These distinguish a *sophisticated* staged launch from a crude one; a
     token can pass every contract check and still show this.
   - Whether snipers already offloaded (retention percentages).
   - Holder concentration and what the top-10 wallets are (check the pair and
     any locker contracts before calling a wallet a whale).
   - Contract risks: owner, proxy, unverified source, suspect functions.
4. If `suspect_source_hits` is non-empty and an explorer API is configured,
   fetch the verified source and read those functions before summarizing —
   a `setTax` capped at 5% is different from an uncapped one.
5. State what could NOT be verified (no explorer API, funding untraced,
   cross-chain funding) — do not fill gaps with assumptions.
6. Never present a low score as a buy recommendation; it only means the rigged-
   launch checks passed.
