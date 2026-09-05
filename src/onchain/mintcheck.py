"""Answer "can anyone mint more of this token?" from verified source + chain state.

The suspect-source flag only reports that the string ``mint(`` appears in the
verified source — which also matches the internal ``_mint(`` every standard
ERC-20 calls once at deploy, so it fires on fixed-supply tokens too. This tool
performs the manual read that flag prescribes: it extracts every mint-named
function header with its visibility and access modifiers, then cross-references
owner state and proxy-ness, because a mint function is only a live threat if
someone can still call it.

Usage:
    python -m src.onchain.mintcheck 0xTOKEN
"""
from __future__ import annotations

import re
import sys

from . import config, explorer
from .erc20 import inspect_token
from .rpc import EvmRpc

# function <name-containing-mint>(args) <modifiers until body or ;>
_HEADER_RE = re.compile(
    r"function\s+([A-Za-z0-9_]*mint[A-Za-z0-9_]*)\s*\(([^)]*)\)\s*([^;{]*)",
    re.IGNORECASE | re.DOTALL)

_ACCESS_HINTS = ("onlyowner", "onlyrole", "only_minter", "onlyminter",
                 "authorized", "restricted", "ownable", "requiresauth")


def _classify(modifiers: str) -> tuple[str, str]:
    """(visibility, access-hint) from a function header's modifier region."""
    m = modifiers.lower()
    if "external" in m:
        vis = "external"
    elif "public" in m:
        vis = "public"
    elif "private" in m:
        vis = "private"
    else:
        # Solidity defaults functions to public only pre-0.5; modern sources
        # always declare it — treat undeclared as internal-ish but say so.
        vis = "internal" if "internal" in m else "undeclared"
    access = next((h for h in _ACCESS_HINTS if h in m), "")
    return vis, access


def run(token: str) -> int:
    rpc = EvmRpc()
    info = inspect_token(rpc, token, include_source=False)
    src = explorer.get_contract_source(token)

    print(f"=== mint check — {info.name} ({info.symbol}) {token} ===")
    print(f"owner: {info.owner or 'none exposed'}"
          + (" (RENOUNCED)" if info.owner_renounced else
             " (NOT renounced)" if info.owner_renounced is False else ""))
    print(f"proxy: {'YES — upgradeable' if info.is_proxy else 'no'}")

    if src is None:
        print("\nexplorer unavailable "
              f"({explorer.LAST_ERROR or 'no explorer configured'}) — "
              "source cannot be read; mint capability UNRESOLVED, not cleared")
        return 2
    if src == "":
        print("\nsource NOT verified — mint capability cannot be checked from "
              "source; unverified source is itself a standing red flag")
        return 2

    callable_fns, internal_fns = [], []
    for name, args, modifiers in _HEADER_RE.findall(src):
        vis, access = _classify(" ".join(modifiers.split()))
        entry = (name, vis, access)
        (callable_fns if vis in ("external", "public", "undeclared")
         else internal_fns).append(entry)

    mint_calls = (len(re.findall(r"[^A-Za-z0-9_]_mint\s*\(", src))
                  - len(re.findall(r"function\s+_mint\s*\(", src)))
    print(f"\n_mint( call sites in source: {mint_calls} "
          "(one inside a constructor = the initial supply, standard)")

    if not callable_fns:
        print("no externally callable mint-named function in the verified "
              "source — supply cannot be inflated through a mint call"
              + (" of THIS logic contract; the proxy admin can swap in one "
                 "that mints" if info.is_proxy else ""))
        if internal_fns:
            print("internal-only mint functions (not directly callable): "
                  + ", ".join(f"{n} [{v}]" for n, v, _ in internal_fns))
        return 1 if info.is_proxy else 0

    print("externally callable mint-named function(s):")
    worst = 0
    for name, vis, access in callable_fns:
        if access:
            if info.owner_renounced and access == "onlyowner":
                note = ("gated by owner — owner is renounced, so this is "
                        "permanently uncallable" +
                        (" UNLESS the proxy logic changes" if info.is_proxy
                         else ""))
                worst = max(worst, 1 if info.is_proxy else 0)
            else:
                note = (f"gated by '{access}' — the holder of that power CAN "
                        "mint; verify who holds it on the explorer")
                worst = 2
        else:
            note = ("NO access modifier detected in the header — read the "
                    "function body: it may be open to anyone")
            worst = 2
        print(f"  {name}(...) [{vis}] — {note}")
    if info.is_proxy:
        print("proxy caveat: whatever the current logic says, the admin can "
              "replace it")
    return worst


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("0x"):
        print(__doc__)
        return 2
    config.require_rpc()
    return run(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
