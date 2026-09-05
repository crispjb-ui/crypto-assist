#!/usr/bin/env python3
"""Target-integrity validator for evm-token-due-diligence formal reports.

Usage:
    python3 validate_report.py <manifest.json>     # validate one manifest
    python3 validate_report.py --selftest          # synthetic rejection cases

Passing validates the report's INTERNAL CONSISTENCY only — not RPC honesty,
discovery completeness, or protocol safety. Stdlib only; no network, no keys.
"""
from __future__ import annotations

import json
import re
import sys

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
PLACEHOLDERS = {"", "todo", "tbd", "...", "0x", "0x0", "n/a", "xxx"}
VALID_RESULTS = {"pass", "fail", "unknown", "coverage-limited", "n/a"}


def _is_placeholder(s: object) -> bool:
    return isinstance(s, str) and s.strip().lower() in PLACEHOLDERS


def _zero_hash(h: str) -> bool:
    return bool(HASH_RE.match(h)) and int(h, 16) == 0


def validate(m: dict) -> list[str]:
    """Return a list of error strings; empty list means internally consistent."""
    errs: list[str] = []

    if not isinstance(m, dict):
        return ["manifest is not a JSON object"]

    target = m.get("target") or {}
    report = m.get("report") or {}
    meta = m.get("metadata") or {}
    pins = m.get("pins") or []
    scope = m.get("scope_addresses") or []
    decls = m.get("declarations") or {}
    checks = m.get("checks") or []

    # --- target ---
    t_addr = target.get("address", "")
    t_chain = target.get("chain_id")
    if not ADDR_RE.match(str(t_addr)):
        errs.append(f"target.address malformed: {t_addr!r}")
    if not isinstance(t_chain, int):
        errs.append(f"target.chain_id must be an int: {t_chain!r}")

    # --- report identity vs target (wrong-target / manifest-source) ---
    if report.get("reported_chain_id") != t_chain:
        errs.append("report.reported_chain_id != target.chain_id "
                    f"({report.get('reported_chain_id')} vs {t_chain})")
    if str(report.get("reported_address", "")).lower() != str(t_addr).lower():
        errs.append("report.reported_address != target.address "
                    f"({report.get('reported_address')} vs {t_addr})")
    if report.get("source_manifest_id") != m.get("manifest_id"):
        errs.append("report.source_manifest_id != manifest_id")

    # --- metadata consistency (present OR explicitly unresolved, not both) ---
    unresolved = set(meta.get("unresolved") or [])
    for field in ("name", "symbol", "decimals", "total_supply"):
        present = field in meta and meta.get(field) not in (None, "")
        if present and field in unresolved:
            errs.append(f"metadata.{field} both present and listed unresolved")
        if not present and field not in unresolved:
            errs.append(f"metadata.{field} missing and not listed unresolved")

    # --- pins ---
    pinned_chains = set()
    if not pins:
        errs.append("no pins")
    for i, p in enumerate(pins):
        blk = p.get("block")
        h = p.get("hash", "")
        utc = p.get("utc", "")
        ch = p.get("chain_id")
        if not isinstance(ch, int):
            errs.append(f"pins[{i}].chain_id must be int")
        else:
            pinned_chains.add(ch)
        if not isinstance(blk, int) or blk <= 0:
            errs.append(f"pins[{i}].block invalid: {blk!r}")
        if _is_placeholder(h) or not HASH_RE.match(str(h)) or _zero_hash(str(h)):
            errs.append(f"pins[{i}].hash placeholder/invalid: {h!r}")
        if not UTC_RE.match(str(utc)):
            errs.append(f"pins[{i}].utc not ISO-8601 Z: {utc!r}")

    # --- scope addresses ---
    by_addr: dict[str, int] = {}
    tsym = str(meta.get("symbol", "")).lower()
    for i, s in enumerate(scope):
        a = str(s.get("address", ""))
        ch = s.get("chain_id")
        if not ADDR_RE.match(a):
            errs.append(f"scope_addresses[{i}].address malformed: {a!r}")
        for req in ("role", "provenance", "runtime_status"):
            if _is_placeholder(s.get(req)):
                errs.append(f"scope_addresses[{i}].{req} missing")
        if not isinstance(ch, int):
            errs.append(f"scope_addresses[{i}].chain_id must be int")
        elif ch not in pinned_chains:
            errs.append(f"scope_addresses[{i}] on chain {ch} with no pin")
        # conflicting identity: same address, different chain
        key = a.lower()
        if key in by_addr and by_addr[key] != ch:
            errs.append(f"scope_addresses conflict: {a} on chains "
                        f"{by_addr[key]} and {ch}")
        by_addr[key] = ch
        # same-symbol substitution: a scope token claims target's symbol but is
        # a different address or chain
        if tsym and str(s.get("symbol", "")).lower() == tsym and (
                key != str(t_addr).lower() or ch != t_chain):
            errs.append(f"scope_addresses[{i}] duplicates target symbol "
                        f"'{tsym}' on a different address/chain "
                        "(same-symbol substitution)")

    # --- safety declarations ---
    if decls.get("no_real_signing") is not True:
        errs.append("declarations.no_real_signing must be true")
    if decls.get("no_broadcast") is not True:
        errs.append("declarations.no_broadcast must be true")

    # --- checks: unknown must not masquerade as pass ---
    for i, c in enumerate(checks):
        if _is_placeholder(c.get("id")):
            errs.append(f"checks[{i}].id missing")
        if _is_placeholder(c.get("proposition")):
            errs.append(f"checks[{i}].proposition missing")
        res = str(c.get("result", "")).lower()
        if res not in VALID_RESULTS:
            errs.append(f"checks[{i}].result invalid: {c.get('result')!r}")
        if res == "pass" and _is_placeholder(c.get("evidence")):
            errs.append(f"checks[{i}] ({c.get('id')}) result=pass with no "
                        "evidence (unknown presented as pass)")

    return errs


# --------------------------------------------------------------------------
# Self-test: a valid baseline plus synthetic manifests that MUST be rejected.
# --------------------------------------------------------------------------
def _baseline() -> dict:
    return {
        "manifest_id": "demo-1",
        "target": {"chain_id": 4663, "address": "0x" + "ab" * 20},
        "metadata": {"name": "Demo", "symbol": "DEMO", "decimals": 18,
                     "total_supply": "1000000000", "unresolved": []},
        "pins": [{"chain_id": 4663, "block": 55176567,
                  "hash": "0x" + "11" * 32, "utc": "2026-09-05T14:01:55Z"}],
        "scope_addresses": [
            {"address": "0x" + "cd" * 20, "chain_id": 4663,
             "role": "launch factory", "provenance": "deployed-runtime",
             "runtime_status": "contract"}],
        "report": {"reported_chain_id": 4663, "reported_address": "0x" + "ab" * 20,
                   "source_manifest_id": "demo-1"},
        "declarations": {"no_real_signing": True, "no_broadcast": True,
                         "fork_writes_only": True},
        "checks": [{"id": "A1", "proposition": "no mint path", "result": "pass",
                    "evidence": "eth_getCode@pin selector scan"}],
    }


def _selftest() -> int:
    import copy
    cases: list[tuple[str, dict, bool]] = []

    cases.append(("valid baseline", _baseline(), True))

    # 1. same-symbol token substituted from another chain
    m = copy.deepcopy(_baseline())
    m["scope_addresses"].append({"address": "0x" + "ef" * 20, "chain_id": 1,
                                 "symbol": "DEMO", "role": "same-symbol token",
                                 "provenance": "explorer",
                                 "runtime_status": "contract"})
    m["pins"].append({"chain_id": 1, "block": 21000000, "hash": "0x" + "22" * 32,
                      "utc": "2026-09-05T14:01:55Z"})
    cases.append(("same-symbol substitution", m, False))

    # 2. report refers to the wrong target
    m = copy.deepcopy(_baseline())
    m["report"]["reported_address"] = "0x" + "99" * 20
    cases.append(("wrong-target report", m, False))

    # 3. fake / inconsistent block pin
    m = copy.deepcopy(_baseline())
    m["pins"][0]["hash"] = "0x0"
    cases.append(("placeholder block pin", m, False))
    m2 = copy.deepcopy(_baseline())
    m2["pins"][0]["hash"] = "0x" + "00" * 32
    cases.append(("zero block hash", m2, False))

    # 4. unknown check presented as a pass
    m = copy.deepcopy(_baseline())
    m["checks"].append({"id": "C1", "proposition": "holder-sized sell",
                        "result": "pass", "evidence": ""})
    cases.append(("unknown-as-pass check", m, False))

    # 5. missing safety declaration
    m = copy.deepcopy(_baseline())
    m["declarations"]["no_broadcast"] = False
    cases.append(("missing no-broadcast declaration", m, False))

    # 6. metadata field neither present nor unresolved
    m = copy.deepcopy(_baseline())
    del m["metadata"]["symbol"]
    cases.append(("metadata gap not marked unresolved", m, False))

    ok = True
    for name, manifest, should_pass in cases:
        errs = validate(manifest)
        passed = not errs
        verdict = "OK" if passed == should_pass else "FAIL"
        if passed != should_pass:
            ok = False
        detail = "accepted" if passed else f"rejected ({len(errs)} err)"
        print(f"[{verdict}] {name}: {detail}")
        if verdict == "FAIL":
            for e in errs:
                print(f"        - {e}")
    print("\nself-test:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--selftest":
        return _selftest()
    try:
        with open(args[0], encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"cannot read manifest: {exc}", file=sys.stderr)
        return 2
    errs = validate(manifest)
    if not errs:
        print("VALID — internally consistent. This does NOT validate RPC "
              "honesty, discovery completeness, or protocol safety.")
        return 0
    print(f"INVALID — {len(errs)} problem(s):")
    for e in errs:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
