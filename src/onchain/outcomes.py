"""Outcome tracking and detector calibration.

    python -m src.onchain.outcomes update   # label past runs with what happened
    python -m src.onchain.outcomes stats    # per-flag precision from the labels

'update' revisits every logged run older than --min-age-hours and measures the
token's current market state via DexScreener:
    rugged  — liquidity or price collapsed ≥90% vs the run-time snapshot
              (or liquidity now < $1k with no snapshot to compare)
    dead    — no tradable pair on the chain anymore
    alive   — pair still exists and did not collapse
    unknown — DexScreener unreachable or no baseline; retried next update

'stats' turns labels into per-flag precision: of the runs where a flag fired,
how many turned out rugged/dead vs alive — plus the misses (rugged tokens the
score called clean). That output is the input to the improve-detectors loop.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from . import config, dexscreener, store

RUG_COLLAPSE_PCT = -90.0
MIN_VIABLE_LIQUIDITY = 1_000.0
CLEAN_SCORE_MAX = 1  # runs scoring <= this count as "called clean"


def _flag_key(flag: dict) -> str:
    """Stable grouping key: the flag's category slug (set in score.py); label
    prefix only as a fallback for runs logged before categories existed."""
    if flag.get("category"):
        return flag["category"]
    label = flag.get("label", "")
    words = []
    for w in label.split():
        if any(c.isdigit() for c in w) or w.startswith("0x"):
            break
        words.append(w)
    return " ".join(words[:5]) or label[:40]


def measure_outcome(token: str, baseline_market: dict | None) -> tuple[str, dict]:
    try:
        pair = dexscreener.best_pair_for_token(token, config.DEXSCREENER_CHAIN_ID or None)
    except Exception as exc:
        return "unknown", {"error": str(exc)}
    if not pair:
        return "dead", {"reason": "no tradable pair on DexScreener"}

    liq = float((pair.get("liquidity") or {}).get("usd") or 0.0)
    price = float(pair.get("priceUsd") or 0.0)
    result = {"liquidity_usd": liq, "price_usd": price}

    base_liq = base_price = None
    if baseline_market:
        base_liq = (baseline_market.get("liquidity") or {}).get("usd")
        try:
            base_price = float(baseline_market.get("priceUsd") or 0) or None
        except (TypeError, ValueError):
            base_price = None

    liq_chg = (100.0 * (liq - base_liq) / base_liq) if base_liq else None
    price_chg = (100.0 * (price - base_price) / base_price) if base_price else None
    result["liquidity_change_pct"] = liq_chg
    result["price_change_pct"] = price_chg

    collapsed = ((liq_chg is not None and liq_chg <= RUG_COLLAPSE_PCT)
                 or (price_chg is not None and price_chg <= RUG_COLLAPSE_PCT)
                 or (base_liq is None and liq < MIN_VIABLE_LIQUIDITY))
    return ("rugged" if collapsed else "alive"), result


def cmd_update(min_age_hours: float) -> None:
    pending = store.runs_awaiting_outcome(min_age_hours)
    if not pending:
        print("No runs awaiting outcomes.")
        return
    for run in pending:
        market = json.loads(run["market_json"]) if run["market_json"] else None
        status, details = measure_outcome(run["token"], market)
        store.record_outcome(
            run["id"], status,
            details.get("price_usd"), details.get("liquidity_usd"),
            details.get("price_change_pct"), details.get("liquidity_change_pct"),
            details,
        )
        print(f"run {run['id']}  {run['token']}  score {run['score']:>2}  -> {status}")
    print(f"\n{len(pending)} run(s) labeled. Now: python -m src.onchain.outcomes stats")


def stats_data() -> dict:
    """Calibration stats as data — shared by the CLI and the dashboard."""
    rows = store.scored_runs_with_outcomes()
    bad_states = {"rugged", "dead"}
    per_flag: dict[str, dict[str, int]] = defaultdict(lambda: {"bad": 0, "alive": 0})
    misses: list[dict] = []
    total_bad = total_alive = 0

    for row in rows:
        is_bad = row["status"] in bad_states
        total_bad += is_bad
        total_alive += not is_bad
        for flag in json.loads(row["flags_json"]):
            key = _flag_key(flag)
            per_flag[key]["bad" if is_bad else "alive"] += 1
        if is_bad and row["score"] <= CLEAN_SCORE_MAX:
            misses.append({"token": row["token"], "score": row["score"],
                           "status": row["status"]})

    flags = []
    for key, counts in sorted(per_flag.items(),
                              key=lambda kv: -(kv[1]["bad"] + kv[1]["alive"])):
        fired = counts["bad"] + counts["alive"]
        flags.append({"flag": key, "fired": fired, "bad": counts["bad"],
                      "alive": counts["alive"],
                      "precision": counts["bad"] / fired if fired else 0.0})
    return {"labeled": len(rows), "bad": total_bad, "alive": total_alive,
            "flags": flags, "misses": misses,
            "clean_score_max": CLEAN_SCORE_MAX}


def cmd_stats() -> None:
    d = stats_data()
    if not d["labeled"]:
        print("No labeled runs yet. Run some reports, wait 24h, then "
              "`python -m src.onchain.outcomes update`.")
        return
    print(f"Labeled runs: {d['labeled']}  (bad: {d['bad']}, alive: {d['alive']})\n")
    print(f"{'flag':50} {'fired':>6} {'bad':>5} {'alive':>6} {'precision':>10}")
    for f in d["flags"]:
        print(f"{f['flag']:50} {f['fired']:>6} {f['bad']:>5} {f['alive']:>6} "
              f"{f['precision']:>9.0%}")
    if d["misses"]:
        print(f"\nMISSES — went bad but scored <= {d['clean_score_max']} (each "
              "one is a detector gap; inspect its stored run data):")
        for m in d["misses"]:
            print(f"  {m['token']}  scored {m['score']}  -> {m['status']}")
    else:
        print("\nNo misses among labeled runs.")
    print("\nInterpretation: low-precision flags over-fire (candidates for "
          "down-weighting); misses are signals the score does not yet measure. "
          "Feed both into /improve-detectors.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Outcome labeling and flag calibration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update", help="label past runs with observed outcomes")
    up.add_argument("--min-age-hours", type=float, default=24.0)
    sub.add_parser("stats", help="per-flag precision from labeled runs")
    args = ap.parse_args()
    if args.cmd == "update":
        cmd_update(args.min_age_hours)
    else:
        cmd_stats()


if __name__ == "__main__":
    main()
