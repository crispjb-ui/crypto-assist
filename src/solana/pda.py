"""Program-derived addresses — stdlib only (sha256 + ed25519 curve check).

Needed to derive a wallet's associated token account (ATA) for a mint
deterministically, so its full transaction history can be pulled even after
the account was closed (getTokenAccountsByOwner only lists open accounts).
"""
from __future__ import annotations

import hashlib

from . import b58

ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_PDA_MARKER = b"ProgramDerivedAddress"

# ed25519 field + curve constant d = -121665/121666
_P = 2**255 - 19
_D = (-121665 * pow(121666, -1, _P)) % _P


def is_on_curve(pk: bytes) -> bool:
    """True when the 32 bytes decompress to a point on ed25519 — PDAs must
    NOT be on the curve (no private key can exist for them)."""
    if len(pk) != 32:
        return False
    y = int.from_bytes(pk, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, -1, _P) % _P
    if x2 == 0:
        return (pk[31] >> 7) == 0        # x=0 valid only with sign bit clear
    return pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    prog = b58.decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog
                           + _PDA_MARKER).digest()
        if not is_on_curve(h):
            return b58.encode(h), bump
    raise ValueError("no viable bump")


def associated_token_address(owner: str, mint: str, token_program: str) -> str:
    addr, _ = find_program_address(
        [b58.decode(owner), b58.decode(token_program), b58.decode(mint)],
        ATA_PROGRAM)
    return addr
