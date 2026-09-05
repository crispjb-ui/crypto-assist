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
from . import outcomes, pons, report, setups, wallets
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


JOBS = {
    "wallets_scan": ("Ingest wallet PnL (24h)",
                     lambda: wallets.scan(hours=24, limit=150)),
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
        result = False
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

    with state["lock"]:
        setup_tokens = {r.get("token") for r in state["setups"]["rows"]
                        if r.get("token")}

    entrypoints: dict = {}
    rows: dict[str, dict] = {}
    for l in pons.recent_launches(rpc, from_block, to_block, deep=True,
                                  limit=60, snipe_window_blocks=snipe_window,
                                  entrypoint_sink=entrypoints):
        smart_hits = smart.intersection(l.snipe_buyers)
        cls = classify_pons(l)
        if smart_hits and cls == "quiet":
            cls = "watch"
        is_setup = l.token in setup_tokens
        insider = l.exempt_buys + len(l.declared_exemptions)
        opp, reason = opportunity(l.taxed_buys, len(smart_hits), l.graduated,
                                  insider, 0, is_setup)
        rows[l.token] = {
            "venue": f"pons v{l.version}", "symbol": l.symbol, "token": l.token,
            "pair": l.curve_or_pool, "block": l.block,
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
        cls = classify_long(l)
        if smart_hits and cls == "quiet":
            cls = "watch"
        is_setup = l.token in setup_tokens
        opp, reason = opportunity(l.early_buyers, len(smart_hits), False,
                                  0, l.offloaded_top, is_setup)
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


def scanner_loop() -> None:
    last_setups = 0.0
    while True:
        state["scanning"] = True
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
                                 "jobs": state["jobs"]})
        elif self.path.startswith("/api/wallets"):
            try:
                everyone = wallets.leaderboard(min_tokens=1)
                # Headline wallets with a real sample (>=2 tokens); a single
                # 100%-win launch is survivorship noise, not an edge.
                repeat = [e for e in everyone if e["tokens"] >= 2]
                # Drop contract addresses (routers/helpers route everyone's
                # trades and are not traders). Verified by code check, cached.
                traders, contracts = [], 0
                for e in repeat:
                    if _is_contract(e["wallet"]):
                        contracts += 1
                    else:
                        traders.append(e)
                smart = wallets.smart_set()
                for e in traders:
                    e["smart"] = e["wallet"] in smart and not _is_contract(e["wallet"])
                self._json(200, {
                    "rows": traders[:100],
                    "smart_count": sum(1 for e in traders if e["smart"]),
                    "repeat_wallet_count": len(traders),
                    "total_tracked": len(everyone),
                    "single_launch_count": len(everyone) - len(repeat),
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
            if not (token.startswith("0x") and len(token) == 42):
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
