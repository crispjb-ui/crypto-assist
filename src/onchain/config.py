"""Env-driven configuration. Loads .env from the repo root if present.

Multi-chain: set CRYPTO_ASSIST_ENV to another profile file (e.g. ".env.bsc")
to run any CLI or the dashboard against a different chain. Each profile is a
complete chain config; ledger rows are tagged with the chain id so calibration
and wallet stats never mix across chains.
"""
import os
from pathlib import Path


def _load_dotenv() -> None:
    root = Path(__file__).resolve().parents[2]
    env = root / os.environ.get("CRYPTO_ASSIST_ENV", ".env")
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.split("#")[0].strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

EVM_RPC_URL = os.environ.get("EVM_RPC_URL", "")
EVM_CHAIN_ID = os.environ.get("EVM_CHAIN_ID", "")
EXPLORER_API_URL = os.environ.get("EXPLORER_API_URL", "").rstrip("/")
EXPLORER_API_KEY = os.environ.get("EXPLORER_API_KEY", "")
DEXSCREENER_CHAIN_ID = os.environ.get("DEXSCREENER_CHAIN_ID", "")
NATIVE_SYMBOL = os.environ.get("NATIVE_SYMBOL", "ETH")

MAX_LOG_BLOCK_RANGE = int(os.environ.get("MAX_LOG_BLOCK_RANGE", "5000"))
# "Early buyer" = bought within this many SECONDS of pair creation; converted
# to blocks at runtime from the measured block rate (fast chains make any
# fixed block count meaningless).
EARLY_WINDOW_SECONDS = float(os.environ.get("EARLY_WINDOW_SECONDS", "60"))
EARLY_WINDOW_BLOCKS = int(os.environ.get("EARLY_WINDOW_BLOCKS", "0"))  # 0 = derive
MAX_EARLY_BUYERS = int(os.environ.get("MAX_EARLY_BUYERS", "40"))


# Tag for ledger rows; '' would mix chains, so fall back to the RPC host.
CHAIN_KEY = EVM_CHAIN_ID or (EVM_RPC_URL.split("//")[-1].split("/")[0]
                             if EVM_RPC_URL else "unconfigured")


def require_rpc() -> str:
    if not EVM_RPC_URL:
        raise SystemExit(
            "EVM_RPC_URL is not set. Copy .env.example to .env and fill in your "
            "RPC endpoint for the target chain."
        )
    return EVM_RPC_URL
