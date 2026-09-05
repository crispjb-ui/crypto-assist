# Target-integrity manifest

A JSON document that pins what a formal report is *about*, so the validator can
check the report's internal consistency before delivery. Passing validates
consistency only — NOT RPC honesty, discovery completeness, or protocol safety.

## Shape

```json
{
  "manifest_id": "size-robinhood-2026-09-05",
  "target": { "chain_id": 4663, "address": "0x0f42...6cef" },
  "metadata": {
    "name": "size it", "symbol": "SIZE", "decimals": 18,
    "total_supply": "1000000000",
    "unresolved": []
  },
  "pins": [
    { "chain_id": 4663, "block": 55176567,
      "hash": "0x88f4181085eaf22afe0b035a041aef23035b2ac0867f6307283aedd732166718",
      "utc": "2026-09-05T14:01:55Z" }
  ],
  "scope_addresses": [
    { "address": "0x7ed5...ec7e", "chain_id": 4663, "role": "launch factory",
      "provenance": "deployed-runtime", "runtime_status": "contract" }
  ],
  "report": {
    "reported_chain_id": 4663, "reported_address": "0x0f42...6cef",
    "source_manifest_id": "size-robinhood-2026-09-05"
  },
  "declarations": {
    "no_real_signing": true, "no_broadcast": true, "fork_writes_only": true
  },
  "checks": [
    { "id": "A1", "proposition": "no mint path in deployed runtime",
      "result": "pass", "evidence": "eth_getCode@pin selector scan" },
    { "id": "C1", "proposition": "holder-sized sell simulated on fork",
      "result": "unknown", "evidence": "" }
  ]
}
```

## Field rules the validator enforces
- `target.address` and every `scope_addresses[].address` are `0x` + 40 hex.
- `target.chain_id` == `report.reported_chain_id`; `target.address` ==
  `report.reported_address` (case-insensitive). Mismatch ⇒ wrong-target reject.
- `report.source_manifest_id` == `manifest_id`.
- Every metadata field is present OR listed in `metadata.unresolved`; a field
  both present and listed unresolved is a conflict.
- Each pin has a real block (int > 0), a 32-byte `0x` hash, and an ISO-8601
  `utc`; placeholders (`0x0`, all-zero hash, `TODO`, `...`) are rejected.
- Each scope address has chain_id, role, provenance, runtime_status.
- No scope address on a chain with no pin (inconsistent scope chain).
- No two scope entries with the same address but conflicting chain_id (conflict).
- A scope address whose symbol/identity duplicates the target on a *different*
  chain/address is a same-symbol-substitution reject.
- `declarations.no_real_signing` and `no_broadcast` present and true.
- Every check has id, proposition, result in
  {pass, fail, unknown, coverage-limited, n/a}; a `pass` with empty evidence is
  rejected (unknown-presented-as-pass).
