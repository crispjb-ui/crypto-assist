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
from . import pons, report
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
}


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

    rows: dict[str, dict] = {}
    for l in pons.recent_launches(rpc, from_block, to_block, deep=True,
                                  limit=60, snipe_window_blocks=snipe_window):
        rows[l.token] = {
            "venue": f"pons v{l.version}", "symbol": l.symbol, "token": l.token,
            "pair": l.curve_or_pool, "block": l.block,
            "cls": classify_pons(l), "graduated": l.graduated,
            "detail": (f"{l.exempt_buys} tax-free snipes "
                       f"({l.exempt_buy_quote / 1e18:.2f}q), "
                       f"{l.taxed_buys} taxed buys"
                       + (f", {len(l.declared_exemptions)} "
                          f"{'declared' if l.exemption_source == 'exact' else 'candidate'}"
                          " exempt wallets" if l.declared_exemptions else "")),
        }
    for l in long_mod.recent_launches(rpc, from_block, to_block, deep=True,
                                      limit=40, window_blocks=long_window):
        rows[l.token] = {
            "venue": f"long/{l.paired_symbol}", "symbol": l.symbol,
            "token": l.token, "pair": long_mod.POOL_MANAGER,
            "creation_block": l.block, "block": l.block,
            "cls": classify_long(l), "graduated": False,
            "detail": (f"{l.early_buys} early buys / {l.early_buyers} wallets, "
                       f"{l.offloaded_top}/10 top offloaded"),
        }

    with state["lock"]:
        state["feed"].update(rows)
        trimmed = sorted(state["feed"].values(), key=lambda r: -r["block"])
        state["feed"] = {r["token"]: r for r in trimmed[:FEED_CACHE_MAX]}
        state["last_scan"] = int(time.time())
        state["last_error"] = None


def scanner_loop() -> None:
    while True:
        state["scanning"] = True
        try:
            scan_once()
        except Exception as exc:
            traceback.print_exc()
            with state["lock"]:
                state["last_error"] = str(exc)
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
                                 "last_error": state["last_error"]})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
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
    port = 8537
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
