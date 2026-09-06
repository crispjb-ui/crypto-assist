"""Base58 (Bitcoin alphabet) — Solana addresses. Stdlib only."""
from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(_ALPHABET[r])
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(out))


def decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _INDEX:
            raise ValueError(f"invalid base58 character {c!r}")
        n = n * 58 + _INDEX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = 0
    for c in s:
        if c == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def looks_like_address(s: str) -> bool:
    """32-byte base58 pubkey shape (Solana address)."""
    if not (32 <= len(s) <= 44):
        return False
    try:
        return len(decode(s)) == 32
    except ValueError:
        return False
