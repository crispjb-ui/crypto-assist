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


# Token-2022 extension type ids (TLV after byte 165 of a mint account).
# The dangerous ones get dedicated MintInfo fields; ids not in this table are
# reported as "type N", never guessed.
EXTENSION_NAMES = {
    1: "transfer-fee-config", 3: "mint-close-authority",
    4: "confidential-transfer-mint", 6: "default-account-state",
    7: "immutable-owner", 8: "memo-transfer", 9: "non-transferable",
    10: "interest-bearing", 11: "cpi-guard", 12: "permanent-delegate",
    14: "transfer-hook", 16: "confidential-transfer-fee-config",
    18: "metadata-pointer", 19: "token-metadata", 20: "group-pointer",
    21: "token-group", 22: "group-member-pointer", 23: "token-group-member",
}
_ACCOUNT_TYPE_OFFSET = 165   # 1 = mint; TLV entries follow


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
    extensions: list[str] = None                 # names/type-N of all present
    transfer_fee_bps: int | None = None          # current fee, basis points
    transfer_fee_max: int | None = None          # per-transfer cap, raw units
    transfer_fee_authority: str | None = None    # can change the fee
    permanent_delegate: str | None = None        # can seize from ANY holder
    transfer_hook_program: str | None = None     # program run on every transfer
    default_state_frozen: bool = False           # new accounts start frozen
    non_transferable: bool = False
    interest_bearing: bool = False
    close_authority: bool = False

    def __post_init__(self):
        if self.extensions is None:
            self.extensions = []


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
    if info.has_extensions:
        _parse_extensions(raw, info)
    return info


def _nonzero_pubkey(data: bytes) -> str | None:
    """OptionalNonZeroPubkey: 32 bytes, all-zero = none."""
    return b58.encode(data) if len(data) == 32 and any(data) else None


def _parse_extensions(raw: bytes, info: MintInfo) -> None:
    """TLV walk: u16 type, u16 length, payload. Byte 165 must mark a mint."""
    if len(raw) <= _ACCOUNT_TYPE_OFFSET or raw[_ACCOUNT_TYPE_OFFSET] != 1:
        return
    i = _ACCOUNT_TYPE_OFFSET + 1
    while i + 4 <= len(raw):
        etype = int.from_bytes(raw[i:i + 2], "little")
        length = int.from_bytes(raw[i + 2:i + 4], "little")
        data = raw[i + 4:i + 4 + length]
        i += 4 + length
        if etype == 0:
            continue
        info.extensions.append(EXTENSION_NAMES.get(etype, f"type {etype}"))
        if etype == 1 and len(data) >= 108:   # TransferFeeConfig
            # authority(32) withdraw_auth(32) withheld(u64) older(18) newer(18)
            info.transfer_fee_authority = _nonzero_pubkey(data[0:32])
            info.transfer_fee_max = int.from_bytes(data[98:106], "little")
            info.transfer_fee_bps = int.from_bytes(data[106:108], "little")
        elif etype == 3:
            info.close_authority = True
        elif etype == 6 and data:              # DefaultAccountState
            info.default_state_frozen = data[0] == 2   # 2 = Frozen
        elif etype == 9:
            info.non_transferable = True
        elif etype == 10:
            info.interest_bearing = True
        elif etype == 12 and len(data) >= 32:  # PermanentDelegate
            info.permanent_delegate = _nonzero_pubkey(data[0:32])
        elif etype == 14 and len(data) >= 64:  # TransferHook
            info.transfer_hook_program = _nonzero_pubkey(data[32:64])
