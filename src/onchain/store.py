"""Run ledger: every diligence verdict is recorded, so detector quality is
measurable against what later happened to the token. SQLite, stdlib only.

The ledger is the raw material of the improvement loop:
  run (verdict at time T)  +  outcome (state at T+24h/72h)  →  per-flag precision.
Lives in data/diligence.db, gitignored (local intelligence, not source).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import config

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "diligence.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,                -- unix time of the run
    token TEXT NOT NULL,
    pair TEXT,
    kind TEXT NOT NULL,                 -- 'report' | 'bundle-only'
    score INTEGER NOT NULL,
    flags_json TEXT NOT NULL,           -- [{points, label}]
    notes_json TEXT NOT NULL,
    market_json TEXT,                   -- DexScreener snapshot at run time
    data_json TEXT                      -- full report payload
);
CREATE TABLE IF NOT EXISTS outcomes (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id),
    ts INTEGER NOT NULL,                -- when the outcome was measured
    status TEXT NOT NULL,               -- 'rugged' | 'alive' | 'dead' | 'unknown'
    price_usd REAL,
    liquidity_usd REAL,
    price_change_pct REAL,              -- vs run-time snapshot, when available
    liquidity_change_pct REAL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_token ON runs(token);
CREATE TABLE IF NOT EXISTS wallet_trades (
    wallet TEXT NOT NULL,
    token TEXT NOT NULL,
    spent REAL NOT NULL DEFAULT 0,      -- quote units (ETH) into the curve
    received REAL NOT NULL DEFAULT 0,   -- quote units back out
    trades INTEGER NOT NULL DEFAULT 0,
    last_ts INTEGER NOT NULL,
    PRIMARY KEY (wallet, token)
);
CREATE TABLE IF NOT EXISTS wallet_scans (
    token TEXT PRIMARY KEY,             -- launches already ingested (idempotence)
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_funders (
    wallet TEXT NOT NULL,               -- traded wallet
    chain TEXT NOT NULL,
    funder TEXT NOT NULL,               -- '' = traced, nothing found on-chain
    via_contract INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, chain)
);
CREATE TABLE IF NOT EXISTS quote_tokens (
    symbol TEXT NOT NULL,               -- quote symbol as stored on trades
    chain TEXT NOT NULL,
    address TEXT NOT NULL,              -- token address, for USD pricing
    PRIMARY KEY (symbol, chain)
);
"""


def _chain_filter(column: str = "chain") -> str:
    """SQL fragment matching current-chain rows ('' = pre-migration legacy)."""
    return f"({column} = ? OR {column} = '')"


def wallet_scan_done(token: str) -> bool:
    with connect() as conn:
        return conn.execute(
            f"SELECT 1 FROM wallet_scans WHERE token = ? AND {_chain_filter()}",
            (token.lower(), config.CHAIN_KEY)).fetchone() is not None


def mark_wallet_scan(token: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO wallet_scans (token, ts, chain) "
                     "VALUES (?, strftime('%s','now'), ?)",
                     (token.lower(), config.CHAIN_KEY))


def upsert_wallet_trade(wallet: str, token: str, spent: float,
                        received: float, trades: int,
                        quote_symbol: str = "ETH") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO wallet_trades (wallet, token, spent, received, trades, "
            "last_ts, quote_symbol, chain) "
            "VALUES (?,?,?,?,?,strftime('%s','now'),?,?) "
            "ON CONFLICT(wallet, token) DO UPDATE SET "
            "spent = spent + excluded.spent, received = received + excluded.received, "
            "trades = trades + excluded.trades, last_ts = excluded.last_ts",
            (wallet.lower(), token.lower(), spent, received, trades,
             quote_symbol, config.CHAIN_KEY),
        )


def remember_wallet_funder(wallet: str, funder: str,
                           via_contract: bool = False) -> None:
    """Record a wallet's first funding source ('' = traced, none found —
    bridged-in). Only successful traces are stored; explorer failures are not."""
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO wallet_funders "
                     "(wallet, chain, funder, via_contract) VALUES (?,?,?,?)",
                     (wallet.lower(), config.CHAIN_KEY, funder.lower(),
                      int(via_contract)))


def wallet_funder_map() -> dict[str, str]:
    """wallet -> funder for the current chain (only traced wallets appear)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT wallet, funder FROM wallet_funders WHERE chain = ?",
            (config.CHAIN_KEY,)).fetchall()
    return dict(rows)


def wallet_token_sets(wallets_list: list[str]) -> dict[str, set[str]]:
    """wallet -> set of tokens traded, for portfolio-overlap comparison."""
    if not wallets_list:
        return {}
    marks = ",".join("?" * len(wallets_list))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT wallet, token FROM wallet_trades WHERE wallet IN ({marks}) "
            f"AND {_chain_filter()}",
            (*[w.lower() for w in wallets_list], config.CHAIN_KEY)).fetchall()
    out: dict[str, set[str]] = {}
    for w, t in rows:
        out.setdefault(w, set()).add(t)
    return out


def remember_quote_token(symbol: str, address: str) -> None:
    """Record which token address a quote symbol refers to on this chain, so
    realized PnL can later be priced in USD without guessing from the symbol."""
    if not symbol or not address:
        return
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO quote_tokens (symbol, chain, "
                     "address) VALUES (?,?,?)",
                     (symbol, config.CHAIN_KEY, address.lower()))


def quote_token_map() -> dict[str, str]:
    """symbol -> token address for the current chain."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, address FROM quote_tokens WHERE chain = ?",
            (config.CHAIN_KEY,)).fetchall()
    return dict(rows)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    # migrations. quote_symbol: Long trades realize PnL in stock tokens, not
    # ETH. chain: multi-chain profiles must not mix calibration or wallet
    # stats; '' marks pre-migration rows, which queries treat as current-chain
    # (all pre-migration data came from the original single-chain install).
    for ddl in (
        "ALTER TABLE wallet_trades ADD COLUMN quote_symbol TEXT NOT NULL DEFAULT 'ETH'",
        "ALTER TABLE runs ADD COLUMN chain TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE wallet_trades ADD COLUMN chain TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE wallet_scans ADD COLUMN chain TEXT NOT NULL DEFAULT ''",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def log_run(token: str, pair: str | None, kind: str, score: int,
            flags: list[dict], notes: list[str],
            market: dict | None, data: dict | None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (ts, token, pair, kind, score, flags_json, "
            "notes_json, market_json, data_json, chain) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), token.lower(), (pair or "").lower() or None, kind,
             score, json.dumps(flags), json.dumps(notes),
             json.dumps(market) if market else None,
             json.dumps(data, default=str) if data else None,
             config.CHAIN_KEY),
        )
        return int(cur.lastrowid)


def record_outcome(run_id: int, status: str, price_usd: float | None,
                   liquidity_usd: float | None, price_change_pct: float | None,
                   liquidity_change_pct: float | None, details: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO outcomes (run_id, ts, status, price_usd, "
            "liquidity_usd, price_change_pct, liquidity_change_pct, details_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, int(time.time()), status, price_usd, liquidity_usd,
             price_change_pct, liquidity_change_pct, json.dumps(details)),
        )


def latest_scores(tokens: list[str]) -> dict[str, int]:
    """Most recent diligence score per token (current chain), for cross-checks."""
    if not tokens:
        return {}
    marks = ",".join("?" * len(tokens))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT token, score FROM runs WHERE token IN ({marks}) "
            f"AND {_chain_filter()} ORDER BY ts",
            (*[t.lower() for t in tokens], config.CHAIN_KEY),
        ).fetchall()
    return {t: s for t, s in rows}


def runs_awaiting_outcome(min_age_hours: float = 24.0) -> list[sqlite3.Row]:
    cutoff = int(time.time() - min_age_hours * 3600)
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT r.* FROM runs r LEFT JOIN outcomes o ON o.run_id = r.id "
            "WHERE r.ts <= ? AND (o.run_id IS NULL OR o.status = 'unknown') "
            f"AND {_chain_filter('r.chain')} ORDER BY r.ts",
            (cutoff, config.CHAIN_KEY),
        ).fetchall()


def scored_runs_with_outcomes() -> list[sqlite3.Row]:
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT r.id, r.token, r.score, r.flags_json, o.status "
            "FROM runs r JOIN outcomes o ON o.run_id = r.id "
            "WHERE o.status IN ('rugged','alive','dead') "
            f"AND {_chain_filter('r.chain')}",
            (config.CHAIN_KEY,),
        ).fetchall()
