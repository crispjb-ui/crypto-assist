"""Local diligence dashboard. Stdlib HTTP server + background scanner.

    python -m src.onchain.server            # then open http://localhost:8537

- A background thread rescans Pons and Long on an interval and keeps a rolling
  cache; the page classifies each launch (bad / watch / quiet) from measured
  signals and auto-refreshes.
- The scan box takes any contract address (CA) and runs the full diligence
  pipeline; results are logged to the outcome ledger like CLI runs.
- Binds to 127.0.0.1 only: the page is for this machine, and quick-scan
  requests spend your RPC quota.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config
from . import long as long_mod
from . import outcomes, pons, prices, report, setups, store, wallets
from .pons import block_near_time
from .rpc import EvmRpc

SCAN_INTERVAL_SECONDS = 120
FEED_WINDOW_HOURS = 1.0
FEED_CACHE_MAX = 400
UI_PATH = Path(__file__).with_name("ui.html")

state = {
    "lock": threading.Lock(),
    "feed": {},          # token -> row dict
    "last_scan": None,
    "last_error": None,
    "scanning": False,
    "entrypoints": {},   # unrecognized Pons launch entrypoints (for exact support)
    "jobs": {},          # name -> {status, started, finished, error, result}
    "setups": {"rows": [], "ts": None},
    "explorer": None,
}

_setups_running = threading.Lock()


def _setups_job() -> None:
    # Background sweep and the manual button must never overlap — two
    # concurrent sweeps double the GeckoTerminal call rate and guarantee 429s.
    if not _setups_running.acquire(blocking=False):
        print("note: setups sweep already running; skipping duplicate",
              flush=True)
        return
    try:
        rows = setups.scan()
        with state["lock"]:
            state["setups"] = {"rows": rows, "ts": int(time.time())}
    finally:
        _setups_running.release()


def _probe_entrypoints_job() -> None:
    """Fetch each unrecognized entrypoint's sample tx and analyze its calldata
    structure — the copyable output is what turns heuristic bundle decoding
    into exact decoding."""
    rpc = EvmRpc()
    with state["lock"]:
        entries = list(state["entrypoints"].values())
    for e in entries:
        try:
            tx = rpc.call("eth_getTransactionByHash", [e["sample_tx"]])
            if isinstance(tx, dict):
                e["probe"] = pons.probe_calldata(tx.get("input", ""))
        except Exception as exc:
            e["probe"] = {"error": _redact(str(exc))}
    with state["lock"]:
        for e in entries:
            state["entrypoints"][f"{e['entrypoint']} {e['selector']}"] = e


def _wallets_scan_job() -> None:
    wallets.scan(hours=24, limit=150)
    try:                       # solana cohort evidence rides the same job
        from ..solana import feed as sol_feed
        traced = sol_feed.trace_smart_funders()
        if traced:
            print(f"solana: funding-traced {traced} smart candidate(s)",
                  flush=True)
    except Exception:
        traceback.print_exc()


JOBS = {
    "wallets_scan": ("Ingest wallet PnL (24h)", _wallets_scan_job),
    "outcomes_update": ("Label outcomes now",
                        lambda: outcomes.cmd_update(min_age_hours=20.0)),
    "setups_scan": ("Scan accumulation setups", _setups_job),
    "probe_entrypoints": ("Analyze entrypoints", _probe_entrypoints_job),
}


def _run_job(name: str) -> None:
    fn = JOBS[name][1]
    job = state["jobs"][name]
    try:
        fn()
        job.update(status="done", finished=int(time.time()))
    except Exception as exc:
        traceback.print_exc()
        job.update(status="failed", finished=int(time.time()), error=str(exc))


def start_job(name: str) -> dict:
    with state["lock"]:
        job = state["jobs"].get(name)
        if job and job.get("status") == "running":
            return {"error": f"{name} is already running"}
        state["jobs"][name] = {"status": "running", "started": int(time.time()),
                               "finished": None, "error": None}
    threading.Thread(target=_run_job, args=(name,), daemon=True).start()
    return {"started": name}


_code_cache: dict[str, bool] = {}
_code_rpc: EvmRpc | None = None


def _is_contract(address: str) -> bool:
    """True if the address has bytecode (a contract, not an EOA trader).
    Cached; a lookup failure is treated as EOA so we never over-exclude."""
    a = address.lower()
    if a in _code_cache:
        return _code_cache[a]
    global _code_rpc
    try:
        _code_rpc = _code_rpc or EvmRpc()
        code = _code_rpc.get_code(a)
        result = bool(code and code != "0x")
    except Exception:
        return False   # treat as EOA this time, but DON'T cache the failure —
                       # a throttled lookup must not mislabel a contract forever
    _code_cache[a] = result
    return result


def _redact(text: str) -> str:
    """Never show the RPC URL (it embeds the API key) in the UI."""
    if config.EVM_RPC_URL:
        text = text.replace(config.EVM_RPC_URL, "<rpc-url>")
    return text


def classify_pons(l) -> str:
    if l.exempt_buys or l.declared_exemptions:
        return "bad"
    if l.taxed_buys:
        return "watch"
    return "quiet"


def classify_long(l) -> str:
    if l.offloaded_top >= 3:
        return "bad"
    if l.early_buys:
        return "watch"
    return "quiet"


def opportunity(demand: int, smart: int, graduated: bool, insider: int,
                offloaded: int, is_setup: bool) -> tuple[float, str]:
    """Rank a launch by research-worthiness from cheap signals already gathered.
    NOT a buy score — it orders the feed so the most interesting rise to the top
    instead of pure recency. Positive: organic demand, smart money, graduation,
    matching accumulation setup. Negative: insider snipes, offload."""
    score = 0.0
    reasons = []
    if smart:
        score += 6 * smart
        reasons.append(f"{smart} smart wallet(s)")
    if is_setup:
        score += 8
        reasons.append("accumulation setup")
    if graduated:
        score += 4
        reasons.append("graduated")
    if demand:
        score += min(demand, 40) * 0.4
        reasons.append(f"{demand} organic buys")
    score -= 3 * insider
    score -= 2 * offloaded
    if insider:
        reasons.append(f"-{insider} insider")
    return score, ", ".join(reasons) or "no activity"


def apply_diligence_score(row: dict) -> None:
    """Fold the latest deep-scan diligence score into the ranking, so a token
    that scanned AVOID can never hold a top candidate slot on cheap signals
    alone (organic demand + smart wallet is exactly what a farmed launch
    manufactures). Idempotent — opp/cls/reason are always re-derived from the
    stored base values, so re-applying after a quick scan never compounds."""
    if "base_opp" not in row:
        row["base_opp"] = row["opp"]
        row["base_cls"] = row["cls"]
        row["base_reason"] = row["reason"]
    opp, cls, reason = row["base_opp"], row["base_cls"], row["base_reason"]
    score = row.get("score")
    if score is not None:
        if score >= 5:
            # README threshold: score >= 5 = treat as a farm until proven
            # otherwise. Reclassify so the candidates-only filter drops it.
            cls = "bad"
            opp -= 3 * score
            reason += (f", deep scan: {'AVOID' if score >= 8 else 'HIGH RISK'}"
                       f" ({score})")
        elif score >= 2:
            opp -= score
            reason += f", deep scan: caution ({score})"
        else:
            reason += ", deep scan: no red flags"
    row["opp"], row["cls"], row["reason"] = round(opp, 1), cls, reason


def scan_once() -> None:
    rpc = EvmRpc()
    to_block = rpc.latest_block()
    from_block = block_near_time(rpc, int(time.time() - FEED_WINDOW_HOURS * 3600))
    snipe_window = rpc.blocks_for_seconds(30, floor=100, cap=2_000)
    long_window = rpc.blocks_for_seconds(300, floor=200, cap=50_000)

    try:
        smart = wallets.smart_set()
    except Exception:
        smart = set()
    try:
        cohort = wallets.cohort_set()
    except Exception:
        cohort = set()

    with state["lock"]:
        setup_tokens = {r.get("token") for r in state["setups"]["rows"]
                        if r.get("token")}

    entrypoints: dict = {}
    rows: dict[str, dict] = {}
    for l in pons.recent_launches(rpc, from_block, to_block, deep=True,
                                  limit=60, snipe_window_blocks=snipe_window,
                                  entrypoint_sink=entrypoints):
        smart_hits = smart.intersection(l.snipe_buyers)
        cohort_hits = cohort.intersection(l.snipe_buyers)
        cls = classify_pons(l)
        if smart_hits and cls == "quiet":
            cls = "watch"
        if cohort_hits:
            cls = "bad"   # an operator cohort sniping = insider launch
        is_setup = l.token in setup_tokens
        insider = (l.exempt_buys + len(l.declared_exemptions)
                   + len(cohort_hits))
        opp, reason = opportunity(l.taxed_buys, len(smart_hits), l.graduated,
                                  insider, 0, is_setup)
        if cohort_hits:
            reason += f", OPERATOR COHORT x{len(cohort_hits)}"
        rows[l.token] = {
            "venue": f"pons v{l.version}", "symbol": l.symbol, "token": l.token,
            "pair": l.curve_or_pool, "block": l.block,
            "creation_block": l.block,   # feed knows the launch block — quick
                                         # scans then skip creation resolution
            "cls": cls, "graduated": l.graduated, "smart": len(smart_hits),
            "setup": is_setup, "opp": round(opp, 1), "reason": reason,
            "detail": (f"{l.exempt_buys} tax-free snipes "
                       f"({l.exempt_buy_quote / 1e18:.2f}q), "
                       f"{l.taxed_buys} taxed buys"
                       + (", {} {} exempt wallets".format(
                              len(l.declared_exemptions),
                              {"exact": "declared",
                               "corroborated": "confirmed"}.get(
                                  l.exemption_source, "candidate"))
                          if l.declared_exemptions else "")
                       + (f" — SMART MONEY x{len(smart_hits)}" if smart_hits else "")),
        }
    for l in long_mod.recent_launches(rpc, from_block, to_block, deep=True,
                                      limit=40, window_blocks=long_window):
        smart_hits = smart.intersection(l.buyer_wallets)
        cohort_hits = cohort.intersection(l.buyer_wallets)
        cls = classify_long(l)
        if smart_hits and cls == "quiet":
            cls = "watch"
        if cohort_hits:
            cls = "bad"
        is_setup = l.token in setup_tokens
        opp, reason = opportunity(l.early_buyers, len(smart_hits), False,
                                  len(cohort_hits), l.offloaded_top, is_setup)
        if cohort_hits:
            reason += f", OPERATOR COHORT x{len(cohort_hits)}"
        rows[l.token] = {
            "venue": f"long/{l.paired_symbol}", "symbol": l.symbol,
            "token": l.token, "pair": long_mod.POOL_MANAGER,
            "creation_block": l.block, "block": l.block,
            "cls": cls, "graduated": False, "smart": len(smart_hits),
            "setup": is_setup, "opp": round(opp, 1), "reason": reason,
            "detail": (f"{l.early_buys} early buys / {l.early_buyers} wallets, "
                       f"{l.offloaded_top}/10 top offloaded"
                       + (f" — SMART MONEY x{len(smart_hits)}" if smart_hits else "")),
        }

    # solana live launch feed — merged into the same ranked list (guarded:
    # no-op without SOLANA_RPC_URL; a solana failure never breaks the EVM feed)
    try:
        from ..solana import feed as sol_feed
        rows.update(sol_feed.scan(opportunity))
    except Exception:
        traceback.print_exc()

    # attach the latest diligence score from the ledger, when one exists,
    # and fold it into the ranking (a scanned-AVOID token must not rank #1)
    try:
        evm = [t for t in rows if t.startswith("0x")]
        sol = [t for t in rows if not t.startswith("0x")]
        scores = store.latest_scores(evm)
        scores.update(store.latest_scores(sol, chain="solana"))
        for token, row in rows.items():
            row["score"] = scores.get(token)
    except Exception:
        pass
    for row in rows.values():
        apply_diligence_score(row)

    with state["lock"]:
        state["feed"].update(rows)
        trimmed = sorted(state["feed"].values(), key=lambda r: -r["block"])
        state["feed"] = {r["token"]: r for r in trimmed[:FEED_CACHE_MAX]}
        # merge, preserving any probe enrichment a previous analysis added
        # (a plain rescan would otherwise wipe it before it can be read)
        for k, v in entrypoints.items():
            prev = state["entrypoints"].get(k)
            if prev and prev.get("probe"):
                v["probe"] = prev["probe"]
            state["entrypoints"][k] = v
        state["last_scan"] = int(time.time())
        state["last_error"] = None


SETUPS_INTERVAL_SECONDS = 1800  # self-identifying setup sweep, every 30 min

# Maintenance self-schedules — no button pressing required. Buttons remain for
# manual re-runs; start_job dedupes if a run is still in flight.
AUTO_JOBS_SECONDS = {
    "wallets_scan": 6 * 3600,
    "outcomes_update": 6 * 3600,
}
_auto_last: dict[str, float] = {}


def _run_due_auto_jobs() -> None:
    now = time.time()
    for name, interval in AUTO_JOBS_SECONDS.items():
        if now - _auto_last.get(name, 0.0) >= interval:
            result = start_job(name)
            if "started" in result:
                _auto_last[name] = now
                print(f"auto-maintenance: {name} started", flush=True)


def scanner_loop() -> None:
    from . import explorer
    last_setups = 0.0
    while True:
        state["scanning"] = True
        try:
            h = explorer.health()
            with state["lock"]:
                state["explorer"] = h
            if not h["ok"]:
                print(f"explorer check FAILED: {h['error']}", flush=True)
        except Exception:
            traceback.print_exc()
        try:
            scan_once()
        except Exception as exc:
            traceback.print_exc()
            with state["lock"]:
                state["last_error"] = _redact(str(exc))
        if time.time() - last_setups >= SETUPS_INTERVAL_SECONDS:
            try:
                _setups_job()
            except Exception:
                traceback.print_exc()
            last_setups = time.time()
        try:
            _run_due_auto_jobs()
        except Exception:
            traceback.print_exc()
        state["scanning"] = False
        time.sleep(SCAN_INTERVAL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str).encode(),
                   "application/json")

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path in ("/", "/index.html"):
            try:
                self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"ui.html missing", "text/plain")
        elif self.path.startswith("/api/feed"):
            with state["lock"]:
                # rank by opportunity, then recency — best rise to the top
                rows = sorted(state["feed"].values(),
                              key=lambda r: (-r.get("opp", 0), -r["block"]))
                self._json(200, {"rows": rows, "last_scan": state["last_scan"],
                                 "scanning": state["scanning"],
                                 "last_error": state["last_error"],
                                 "entrypoints": list(state["entrypoints"].values()),
                                 "jobs": state["jobs"],
                                 "explorer": state["explorer"]})
        elif self.path.startswith("/api/wallets"):
            try:
                everyone, traders, contracts = [], [], 0
                # per-chain: leaderboard, smart bar, cohorts, USD pricing —
                # never mixed (chain='solana' for sol; contract filtering is
                # EVM-only: solana fee payers are user wallets, not programs)
                chains = [("evm", None, None, None)]
                import os as _os
                if _os.environ.get("SOLANA_RPC_URL"):
                    chains.append(("sol", "solana", "solana", "solana"))
                for tag, chain, dex_chain, store_chain in chains:
                    board = wallets.leaderboard(min_tokens=1, chain=chain)
                    everyone.extend(board)
                    # Headline wallets with a real sample (>=2 tokens); a
                    # single 100%-win launch is survivorship noise.
                    repeat = [e for e in board if e["tokens"] >= 2]
                    kept = []
                    for e in repeat:
                        if tag == "evm" and _is_contract(e["wallet"]):
                            contracts += 1
                        else:
                            e["chain"] = tag
                            kept.append(e)
                    smart = wallets.smart_set(chain=chain)
                    cohorts = wallets.detect_cohorts(
                        wallets.smart_candidates(chain=chain), chain=chain)
                    quote_syms = sorted({q for e in kept
                                         for q in e["realized_by_quote"]})
                    try:
                        px = prices.quote_prices_usd(
                            quote_syms, dex_chain=dex_chain,
                            store_chain=store_chain)
                    except Exception:
                        px = {}
                    for e in kept:
                        e["smart"] = e["wallet"] in smart
                        e["cohort"] = cohorts.get(e["wallet"], 0)
                        usd, unpriced = prices.usd_realized(
                            e["realized_by_quote"], px)
                        e["usd_realized"] = (round(usd, 2)
                                             if usd is not None else None)
                        e["usd_unpriced"] = unpriced
                    traders.extend(kept)
                traders.sort(key=lambda e: (-e["win_rate"],
                                            -(e["usd_realized"] or 0)))
                funders_traced = len(store.wallet_funder_map())
                try:
                    funders_traced += len(store.wallet_funder_map(chain="solana"))
                except Exception:
                    pass
                self._json(200, {
                    "rows": traders[:100],
                    "smart_count": sum(1 for e in traders if e["smart"]),
                    "cohort_wallet_count": sum(1 for e in traders if e["cohort"]),
                    "funders_traced": funders_traced,
                    "repeat_wallet_count": len(traders),
                    "total_tracked": len(everyone),
                    "single_launch_count": len(everyone) - len(traders)
                                           - contracts,
                    "contracts_excluded": contracts,
                })
            except Exception as exc:
                self._json(500, {"error": str(exc)})
        elif self.path.startswith("/api/stats"):
            try:
                self._json(200, outcomes.stats_data())
            except Exception as exc:
                self._json(500, {"error": str(exc)})
        elif self.path.startswith("/api/setups"):
            with state["lock"]:
                self._json(200, state["setups"])
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/job":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
                name = req.get("name")
                if name not in JOBS:
                    self._json(400, {"error": f"unknown job; valid: {list(JOBS)}"})
                else:
                    self._json(200, start_job(name))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return
        if self.path != "/api/scan":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
            token = (req.get("token") or "").strip()
            if not token.startswith("0x"):
                # Solana mint? Route to the solana pipeline (same scan box).
                from ..solana import b58 as _b58
                from ..solana import report as _sol
                if _b58.looks_like_address(token):
                    try:
                        payload = _sol.collect(token)
                        payload["text"] = _sol.render_text(payload)
                        self._json(200, payload)
                    except _sol.NoPairError as exc:
                        self._json(404, {"error": str(exc)})
                    except SystemExit as exc:   # SOLANA_RPC_URL missing
                        self._json(400, {"error": str(exc)})
                    except Exception as exc:
                        traceback.print_exc()
                        self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                    return
                self._json(400, {"error": "enter a 42-character 0x address "
                                          "or a Solana mint (base58)"})
                return
            if len(token) != 42:
                self._json(400, {"error": "enter a 42-character 0x address"})
                return
            pair = (req.get("pair") or "").strip() or None
            creation_block = req.get("creation_block") or None
            if not pair:
                with state["lock"]:
                    cached = state["feed"].get(token.lower())
                if cached:
                    pair = cached["pair"]
                    creation_block = cached.get("creation_block")
            payload = report.collect(
                token, pair, bundle_only=bool(req.get("bundle_only")),
                creation_block=int(creation_block) if creation_block else None)
            # re-rank the feed row right away — a quick scan that came back
            # AVOID must fall out of the top slots before the next feed cycle
            sc = (payload.get("verdict") or {}).get("score")
            if sc is not None:
                with state["lock"]:
                    row = state["feed"].get(token.lower())
                    if row is not None:
                        row["score"] = sc
                        apply_diligence_score(row)
            self._json(200, payload)
        except report.NoPairError as exc:
            self._json(404, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):  # quiet request logging
        pass


def main() -> None:
    import os
    port = int(os.environ.get("CRYPTO_ASSIST_PORT", "8537"))
    threading.Thread(target=scanner_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"crypto-assist dashboard: http://localhost:{port}")
    print("first feed results appear after the initial scan (~1-3 min); "
          "quick scans work immediately. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


if __name__ == "__main__":
    main()
