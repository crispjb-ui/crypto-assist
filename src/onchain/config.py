"""Env-driven configuration. Loads .env from the repo root if present."""
import os
from pathlib import Path


def _load_dotenv() -> None:
    root = Path(__file__).resolve().parents[2]
    env = root / ".env"
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
EARLY_WINDOW_BLOCKS = int(os.environ.get("EARLY_WINDOW_BLOCKS", "20"))
MAX_EARLY_BUYERS = int(os.environ.get("MAX_EARLY_BUYERS", "40"))


def require_rpc() -> str:
    if not EVM_RPC_URL:
        raise SystemExit(
            "EVM_RPC_URL is not set. Copy .env.example to .env and fill in your "
            "RPC endpoint for the target chain."
        )
    return EVM_RPC_URL
