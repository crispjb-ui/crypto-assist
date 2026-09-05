"""ERC-20 metadata and contract red-flag checks via raw eth_call + explorer API."""
from __future__ import annotations

from dataclasses import dataclass, field

from .rpc import EvmRpc
from .explorer import get_contract_source

# Standard function selectors (keccak-256 of the signature, first 4 bytes).
SEL_NAME = "0x06fdde03"          # name()
SEL_SYMBOL = "0x95d89b41"        # symbol()
SEL_DECIMALS = "0x313ce567"      # decimals()
SEL_TOTAL_SUPPLY = "0x18160ddd"  # totalSupply()
SEL_BALANCE_OF = "0x70a08231"    # balanceOf(address)
SEL_OWNER = "0x8da5cb5b"         # owner()

# EIP-1967 implementation slot (proxy detection).
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

DEAD_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

# Source-code patterns that warrant a manual read. Presence is a flag, not a verdict.
SUSPECT_SOURCE_PATTERNS = [
    "blacklist", "blocklist", "isbot", "_isexcluded", "setmaxtx",
    "settax", "setfee", "enabletrading", "opentrading", "mint(",
    "setswapenabled", "pausetrading",
]


@dataclass
class TokenInfo:
    address: str
    name: str = "?"
    symbol: str = "?"
    decimals: int = 18
    total_supply: int = 0
    owner: str | None = None
    owner_renounced: bool | None = None
    is_proxy: bool = False
    source_verified: bool | None = None  # None = no explorer configured
    suspect_source_hits: list[str] = field(default_factory=list)


def _decode_string(hexdata: str) -> str:
    if not hexdata or hexdata == "0x":
        return "?"
    raw = bytes.fromhex(hexdata[2:])
    try:
        if len(raw) >= 64:  # ABI-encoded dynamic string
            length = int.from_bytes(raw[32:64], "big")
            return raw[64 : 64 + length].decode("utf-8", "replace")
        return raw.rstrip(b"\x00").decode("utf-8", "replace")  # bytes32-style
    except Exception:
        return "?"


def _decode_uint(hexdata: str) -> int:
    if not hexdata or hexdata == "0x":
        return 0
    return int(hexdata, 16)


def _decode_address(hexdata: str) -> str | None:
    if not hexdata or hexdata == "0x" or len(hexdata) < 42:
        return None
    return "0x" + hexdata[-40:]


def _try_call(rpc: EvmRpc, to: str, data: str) -> str:
    """eth_call that treats a revert as 'function not present' — many tokens
    legitimately lack owner() etc., and a revert must not kill the report."""
    from .rpc import RpcError
    try:
        return rpc.eth_call(to, data)
    except RpcError:
        return "0x"


def balance_of(rpc: EvmRpc, token: str, holder: str) -> int:
    data = SEL_BALANCE_OF + holder.lower().replace("0x", "").rjust(64, "0")
    return _decode_uint(_try_call(rpc, token, data))


def inspect_token(rpc: EvmRpc, token: str) -> TokenInfo:
    info = TokenInfo(address=token.lower())
    info.name = _decode_string(_try_call(rpc, token, SEL_NAME))
    info.symbol = _decode_string(_try_call(rpc, token, SEL_SYMBOL))
    info.decimals = _decode_uint(_try_call(rpc, token, SEL_DECIMALS)) or 18
    info.total_supply = _decode_uint(_try_call(rpc, token, SEL_TOTAL_SUPPLY))

    owner = _decode_address(_try_call(rpc, token, SEL_OWNER))
    info.owner = owner
    if owner is not None:
        info.owner_renounced = owner in DEAD_ADDRESSES

    from .rpc import RpcError
    try:
        impl = rpc.get_storage_at(token, EIP1967_IMPL_SLOT)
    except RpcError:
        impl = "0x"
    info.is_proxy = bool(impl and impl != "0x" and int(impl, 16) != 0)

    src = get_contract_source(token)
    if src is not None:
        info.source_verified = bool(src)
        lowered = src.lower()
        info.suspect_source_hits = sorted(
            {p for p in SUSPECT_SOURCE_PATTERNS if p in lowered}
        )
    return info
