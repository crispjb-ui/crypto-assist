"""SPL mint account parsing — the Solana equivalent of the EVM contract flags.

A mint's authorities ARE the rug surface: an active mint authority can print
supply (the EVM 'dev can mint' case, but readable as a fact, not a source
grep), and an active freeze authority can freeze any holder's token account
(the honeypot equivalent). Both revoked = fixed supply, unfreezable.

SPL mint layout (82 bytes):
  0..4    COption tag for mint_authority (0 = none, 1 = some)
  4..36   mint_authority pubkey
  36..44  supply u64 LE
  44      decimals u8
  45      is_initialized
  46..50  COption tag for freeze_authority
  50..82  freeze_authority pubkey
Token-2022 mints share this base layout; bytes beyond 82 are extensions
(transfer fees/hooks live there) and are flagged for manual reading.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from . import b58

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class MintInfo:
    mint: str
    owner_program: str = ""
    decimals: int = 0
    supply: int = 0
    mint_authority: str | None = None    # None = revoked (good)
    freeze_authority: str | None = None  # None = revoked (good)
    is_token_2022: bool = False
    has_extensions: bool = False


def _coption_pubkey(raw: bytes, tag_off: int, key_off: int) -> str | None:
    tag = int.from_bytes(raw[tag_off:tag_off + 4], "little")
    if tag == 0:
        return None
    return b58.encode(raw[key_off:key_off + 32])


def parse_mint(mint: str, account: dict) -> MintInfo:
    """`account` = getAccountInfo value with base64 encoding."""
    info = MintInfo(mint=mint, owner_program=account.get("owner", ""))
    data_field = account.get("data")
    raw = base64.b64decode(data_field[0]) if isinstance(data_field, list) else b""
    if len(raw) < 82:
        raise ValueError(f"account data too short for a mint ({len(raw)} bytes)")
    info.mint_authority = _coption_pubkey(raw, 0, 4)
    info.supply = int.from_bytes(raw[36:44], "little")
    info.decimals = raw[44]
    info.freeze_authority = _coption_pubkey(raw, 46, 50)
    info.is_token_2022 = info.owner_program == TOKEN_2022_PROGRAM
    info.has_extensions = info.is_token_2022 and len(raw) > 82
    return info
