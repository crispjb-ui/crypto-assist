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

def _setups_job() -> None:
    rows = setups.scan()
    with state["lock"]:
        state["setups"] = {"rows": rows, "ts": int(time.time())}


JOBS = {
    "wallets_scan": ("Ingest wallet PnL (24h)",
                     lambda: wallets.scan(hours=24, limit=150)),
    "outcomes_update": ("Label outcomes now",
                        lambda: outcomes.cmd_update(min_age_hours=20.0)),
    "setups_scan": ("Scan accumulation setups", _setups_job),
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

    entrypoints: dict = {}
    rows: dict[str, dict] = {}
    for l in pons.recent_launches(rpc, from_block, to_block, deep=True,
                                  limit=60, snipe_window_blocks=snipe_window,
                                  entrypoint_sink=entrypoints):
        smart_hits = smart.intersection(l.snipe_buyers)
        cls = classify_pons(l)
        if smart_hits and cls == "quiet":
            cls = "watch"
        rows[l.token] = {
            "venue": f"pons v{l.version}", "symbol": l.symbol, "token": l.token,
            "pair": l.curve_or_pool, "block": l.block,
            "cls": cls, "graduated": l.graduated, "smart": len(smart_hits),
            "detail": (f"{l.exempt_buys} tax-free snipes "
                       f"({l.exempt_buy_quote / 1e18:.2f}q), "
                       f"{l.taxed_buys} taxed buys"
                       + (f", {len(l.declared_exemptions)} "
                          f"{'declared' if l.exemption_source == 'exact' else 'candidate'}"
                          " exempt wallets" if l.declared_exemptions else "")
                       + (f" — SMART MONEY x{len(smart_hits)}" if smart_hits else "")),
        }
    for l in long_mod.recent_launches(rpc, from_block, to_block, deep=True,
                                      limit=40, window_blocks=long_window):
        smart_hits = smart.intersection(l.buyer_wallets)
        cls = classify_long(l)
        if smart_hits and cls == "quiet":
            cls = "watch"
        rows[l.token] = {
            "venue": f"long/{l.paired_symbol}", "symbol": l.symbol,
            "token": l.token, "pair": long_mod.POOL_MANAGER,
            "creation_block": l.block, "block": l.block,
            "cls": cls, "graduated": False, "smart": len(smart_hits),
            "detail": (f"{l.early_buys} early buys / {l.early_buyers} wallets, "
                       f"{l.offloaded_top}/10 top offloaded"
                       + (f" — SMART MONEY x{len(smart_hits)}" if smart_hits else "")),
        }

    with state["lock"]:
        state["feed"].update(rows)
        trimmed = sorted(state["feed"].values(), key=lambda r: -r["block"])
        state["feed"] = {r["token"]: r for r in trimmed[:FEED_CACHE_MAX]}
        state["entrypoints"].update(entrypoints)
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
                state["last_error"] = str(exc)
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
                rows = sorted(state["feed"].values(), key=lambda r: -r["block"])
                self._json(200, {"rows": rows, "last_scan": state["last_scan"],
                                 "scanning": state["scanning"],
                                 "last_error": state["last_error"],
                                 "entrypoints": list(state["entrypoints"].values()),
                                 "jobs": state["jobs"]})
        elif self.path.startswith("/api/wallets"):
            try:
                board = wallets.leaderboard(min_tokens=1)
                smart = wallets.smart_set()
                for e in board:
                    e["smart"] = e["wallet"] in smart
                self._json(200, {"rows": board[:100], "smart_count": len(smart)})
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
