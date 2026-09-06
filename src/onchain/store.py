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


def chain_clause(chain: str | None, column: str = "chain") -> tuple[str, str]:
    """(sql, param) selecting one chain's rows. Legacy '' rows all came from
    the original single-chain (EVM) install, so they count as the configured
    EVM chain only — never as an explicitly named other chain (solana)."""
    key = chain or config.CHAIN_KEY
    if key == config.CHAIN_KEY:
        return _chain_filter(column), key
    return f"{column} = ?", key


def _key(addr: str) -> str:
    """Normalize an address for keying: EVM lowercases; base58 stays as-is
    (Solana addresses are case-sensitive)."""
    return addr.lower() if addr.startswith("0x") else addr


def wallet_scan_done(token: str, chain: str | None = None) -> bool:
    sql, key = chain_clause(chain)
    with connect() as conn:
        return conn.execute(
            f"SELECT 1 FROM wallet_scans WHERE token = ? AND {sql}",
            (_key(token), key)).fetchone() is not None


def mark_wallet_scan(token: str, chain: str | None = None) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO wallet_scans (token, ts, chain) "
                     "VALUES (?, strftime('%s','now'), ?)",
                     (_key(token), chain or config.CHAIN_KEY))


def upsert_wallet_trade(wallet: str, token: str, spent: float,
                        received: float, trades: int,
                        quote_symbol: str = "ETH",
                        chain: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO wallet_trades (wallet, token, spent, received, trades, "
            "last_ts, quote_symbol, chain) "
            "VALUES (?,?,?,?,?,strftime('%s','now'),?,?) "
            "ON CONFLICT(wallet, token) DO UPDATE SET "
            "spent = spent + excluded.spent, received = received + excluded.received, "
            "trades = trades + excluded.trades, last_ts = excluded.last_ts",
            (_key(wallet), _key(token), spent, received, trades,
             quote_symbol, chain or config.CHAIN_KEY),
        )


def remember_wallet_funder(wallet: str, funder: str,
                           via_contract: bool = False,
                           chain: str | None = None) -> None:
    """Record a wallet's first funding source ('' = traced, none found —
    bridged-in). Only successful traces are stored; explorer failures are not."""
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO wallet_funders "
                     "(wallet, chain, funder, via_contract) VALUES (?,?,?,?)",
                     (_key(wallet), chain or config.CHAIN_KEY, _key(funder),
                      int(via_contract)))


def wallet_funder_map(chain: str | None = None) -> dict[str, str]:
    """wallet -> funder for one chain (only traced wallets appear)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT wallet, funder FROM wallet_funders WHERE chain = ?",
            (chain or config.CHAIN_KEY,)).fetchall()
    return dict(rows)


def wallet_token_sets(wallets_list: list[str],
                      chain: str | None = None) -> dict[str, set[str]]:
    """wallet -> set of tokens traded, for portfolio-overlap comparison."""
    if not wallets_list:
        return {}
    sql, key = chain_clause(chain)
    marks = ",".join("?" * len(wallets_list))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT wallet, token FROM wallet_trades WHERE wallet IN ({marks}) "
            f"AND {sql}",
            (*[_key(w) for w in wallets_list], key)).fetchall()
    out: dict[str, set[str]] = {}
    for w, t in rows:
        out.setdefault(w, set()).add(t)
    return out


def remember_quote_token(symbol: str, address: str,
                         chain: str | None = None) -> None:
    """Record which token address a quote symbol refers to on one chain, so
    realized PnL can later be priced in USD without guessing from the symbol."""
    if not symbol or not address:
        return
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO quote_tokens (symbol, chain, "
                     "address) VALUES (?,?,?)",
                     (symbol, chain or config.CHAIN_KEY, _key(address)))


def quote_token_map(chain: str | None = None) -> dict[str, str]:
    """symbol -> token address for one chain."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, address FROM quote_tokens WHERE chain = ?",
            (chain or config.CHAIN_KEY,)).fetchall()
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
            market: dict | None, data: dict | None,
            chain: str | None = None) -> int:
    """chain override lets non-EVM stacks (solana) tag their rows so
    calibration never mixes chains; default = the configured EVM chain.
    Solana addresses are case-sensitive base58, so only 0x tokens lowercase."""
    tok = token.lower() if token.startswith("0x") else token
    pr = (pair or "")
    pr = (pr.lower() if pr.startswith("0x") else pr) or None
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (ts, token, pair, kind, score, flags_json, "
            "notes_json, market_json, data_json, chain) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), tok, pr, kind,
             score, json.dumps(flags), json.dumps(notes),
             json.dumps(market) if market else None,
             json.dumps(data, default=str) if data else None,
             chain or config.CHAIN_KEY),
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


def latest_scores(tokens: list[str], chain: str | None = None) -> dict[str, int]:
    """Most recent diligence score per token (one chain), for cross-checks."""
    if not tokens:
        return {}
    sql, key = chain_clause(chain)
    marks = ",".join("?" * len(tokens))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT token, score FROM runs WHERE token IN ({marks}) "
            f"AND {sql} ORDER BY ts",
            (*[_key(t) for t in tokens], key),
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
