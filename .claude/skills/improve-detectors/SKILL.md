---
name: improve-detectors
description: Calibrate and improve the diligence detectors against recorded outcomes. Use when the user asks to improve/tune/calibrate detection, review detector accuracy, or at the end of a session where diligence runs were made.
---

# Detector improvement loop

The backend is deterministic Python (`src/onchain/`); this skill is the
feedback loop that improves it using evidence, never vibes.

## Procedure

1. **Label**: `python -m src.onchain.outcomes update` — measures what happened
   to every token diligenced ≥24h ago (rugged / dead / alive vs its run-time
   snapshot).
2. **Calibrate**: `python -m src.onchain.outcomes stats` — per-flag precision
   and the MISSES list (tokens that went bad but scored clean).
3. **Act on the evidence, in priority order**:
   - **A miss** (rugged token scored ≤1): read its stored run JSON
     (`data/diligence.db`, table `runs`, column `data_json`). Find a
     measurable signal that was present but unscored. Add a detector or flag
     for it in the relevant module + `score.py`.
   - **A low-precision flag** (fires often on tokens that stay alive):
     reduce its points in `score.py` or tighten its threshold.
   - **A saturated flag** (100% precision over ≥10 firings): consider raising
     its points — it is close to proof.
4. **Guard rails for any code change**:
   - Detection stays deterministic: pure functions of on-chain/API data. No
     model judgment inside `score.py`.
   - Add or extend an offline unit check for the change (the repo pattern:
     synthetic inputs, assert flags/score), run `python -m compileall -q src/`
     plus the checks, and only then commit with a message stating the evidence
     ("flag X: 2/9 precision over 30 days → points 3→1").
   - Thresholds move on ≥5 labeled examples, never on 1.
   - Never delete or rewrite ledger history (`data/diligence.db`); it is the
     accumulated training data.
5. **Report** to the user: what was measured, what changed, what needs more
   data before a decision is justified.

## When invoked with too little data

If `stats` shows <10 labeled runs, say so, change nothing, and tell the user
the loop needs more diligence runs + 24h of elapsed time to be meaningful.
