# Output standard

## Lead
Open with a direct, **conditional** verdict answering the user's actual
question — not a generic score. Example leads:
- "Focused answer: the vault's balance reconciles to LP-fee collections for
  X of Y; the remaining Z is unexplained at the pin."
- "NO-GO under the stated rug-resistance requirement: an upgradeable hook can
  redirect fees and gate transfers at the pinned block."

## Rate these surfaces separately
Never average a critical finding away with unrelated positive checks.
- Token controls
- Canonical LP-principal custody
- Side-pool removal risk
- Sellability and exit depth
- Current concentration
- Historical launch integrity
- Admin, treasury, and reward custody
- Reward accounting and liveness
- Utility and redemption rights
- External dependencies
- Development and disclosure

For broad reports, give each: **severity, likelihood, confidence, coverage,
time basis**.

## Bounded language (use, don't overclaim)
- "No current executable removal path found at the pinned block."
- "Sellable at the tested sizes under the quoted state."
- "Unknown because historical state was unavailable."
- "NO-GO under the stated requirement for rug resistance."
Never issue an unconditional "safe", and never imply a favorable review
predicts returns.

## Explain
State the main reasons, the strongest contrary evidence, unresolved questions,
and the specific evidence that could change the conclusion. Recommendations
must address the observed deficiencies.

## Finding-to-evidence ledger (every material finding)
```
finding_id:
proposition:          # exact, falsifiable
chain / address:
pin_or_tx:            # block+hash+utc, or tx hash
artifact / query:     # method + params (credentials redacted)
decoding_basis:       # ABI / selector / event topic
evidence_type:        # proven | strongly-supported | inference | unknown
confidence:
alternatives:         # competing explanations not ruled out
coverage:             # what was and wasn't searched
staleness_conditions: # what would make this claim no longer true
```
For discovery claims also record: search universe, block ranges / pagination,
inclusion rules, materiality thresholds.
