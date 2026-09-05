"""Pons launchpad (ponsfamily.com) watcher for Robinhood Chain.

Pons is the dominant token launchpad on Robinhood Chain. Its V2 flow carries a
snipe tax (99% decaying over ~15s at current factory settings) — but the launch
call can declare up to 32 wallets EXEMPT from that tax. That declared list is
the bundle: the operator's sniper cluster, published in the launch transaction
itself. This module extracts it.

Contract addresses and event/function signatures come from the official
contracts repo (github.com/ponsdotdev/ponsfamily); topic0/selector hashes were
computed from those signatures and cross-checked against known Ethereum hashes.

    python -m src.onchain.pons --hours 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict

from .erc20 import _decode_string, SEL_SYMBOL
from .rpc import EvmRpc

# Deployed factories on Robinhood Chain (chain id 4663), per the official repo.
V1_FACTORY = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
V2_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"

# topic0 hashes (keccak-256 of the event signature)
# TokenLaunched(address,address,address,address,address,uint256,uint256,uint256,uint256,uint256)
TOPIC_V1_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
# TokenLaunched(address,address,address,address,uint256,uint256)
TOPIC_V2_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
# PoolGraduated(address,uint256,uint256,uint256)
TOPIC_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"
# CurveBuy(address,address,uint256,uint256,uint256,uint256)
TOPIC_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
# CurveSell(address,address,uint256,uint256,uint256,uint256)
TOPIC_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"

# Function selectors on PonsV2LaunchFactory (TokenParams =
# (string,string,string,string,(string,string,string,string,string),address,uint16,bool,bytes32))
SEL_LAUNCH_PLAIN = "0xa41d5f2b"   # launchToken(TokenParams,uint256,address)
SEL_LAUNCH_EXEMPT = "0x3580febb"  # launchToken(TokenParams,uint256,address,address[])
SEL_LAUNCH_FOR = "0x42236f86"     # launchTokenFor(TokenParams,uint256,address,address,address[])

# Launch entrypoints confirmed from live Robinhood Chain data that are NOT in
# the published repo (deployed factory ABI drift + front-end routers). Recorded
# as (entrypoint_lower, selector); entrypoint "*" means any caller with that
# selector (router-family). Presence here suppresses the "unrecognized" warning
# and marks heuristic bundle extraction as trusted — exact arg-slot decoding
# still needs a calldata probe (see probe_calldata / the dashboard job).
KNOWN_LAUNCH_ENTRYPOINTS = {
    (V2_FACTORY.lower(), "0xf35abbcf"),   # factory-direct launch (deployed ABI)
    (V2_FACTORY.lower(), "0xa72101af"),   # factory-direct launch (deployed ABI)
    ("0xe33e9e479df8802cb0866d5d05258bec4cf62948", "0xf85f8e41"),  # dominant router
}
KNOWN_SELECTORS = {sel for _, sel in KNOWN_LAUNCH_ENTRYPOINTS}


def is_known_entrypoint(to: str, selector: str) -> bool:
    to = (to or "").lower()
    return ((to, selector) in KNOWN_LAUNCH_ENTRYPOINTS
            or selector in KNOWN_SELECTORS)


@dataclass
class PonsLaunch:
    version: int
    token: str
    deployer: str
    curve_or_pool: str            # V2: bonding curve; V1: Uniswap V3 pool
    block: int
    tx_hash: str
    symbol: str = "?"
    declared_exemptions: list[str] = field(default_factory=list)
    # "exact": decoded from a known factory selector; "heuristic": address[]
    # recovered from unrecognized (router) calldata; "opaque": nothing found.
    exemption_source: str = "exact"
    graduated: bool = False
    # snipe-window trading (V2 only)
    exempt_buys: int = 0           # tax-free buys in the window = the bundle executing
    taxed_buys: int = 0            # outsiders paying the decaying tax
    exempt_buy_quote: int = 0      # total quote spent by tax-free buyers (wei)
    snipe_buyers: list[str] = field(default_factory=list)  # distinct window buyers
    # wallets that actually bought tax-free (non-deployer): the bundle as
    # PROVEN by execution, independent of any calldata decoding
    exempt_buyer_wallets: list[str] = field(default_factory=list)


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _looks_like_address_word(w: str) -> bool:
    return len(w) == 64 and w[:24] == "0" * 24 and int(w[24:], 16) != 0


def _heuristic_address_array(body: str) -> list[str]:
    """Best-effort recovery of the trailing address[] from unrecognized (router)
    calldata: find length-prefixed runs of address-shaped words, take the last.
    Launch entrypoints put snipeTaxExemptions last in every known layout."""
    words = [body[i : i + 64] for i in range(0, len(body) - 63, 64)]
    best: list[str] = []
    for i, w in enumerate(words):
        try:
            n = int(w, 16)
        except ValueError:
            continue
        if not (1 <= n <= 32) or i + n >= len(words) + 1:
            continue
        run = words[i + 1 : i + 1 + n]
        if len(run) == n and all(_looks_like_address_word(x) for x in run):
            # reject if the "length" word is itself part of a longer address run
            # (offset words like 0x20 precede structs, not just arrays)
            best = ["0x" + x[-40:] for x in run]
    return best


def exemptions_from_calldata(data: str) -> tuple[list[str], str]:
    """Extract the declared snipe-tax exemption list from a V2 launch tx.

    Returns (addresses, source): "exact" for known factory selectors,
    "heuristic" for an address[] recovered from unrecognized router calldata,
    "opaque" when nothing was found — absence of evidence, not evidence of
    absence.
    """
    sel = data[:10].lower()
    body = data[10:]

    def word(i: int) -> str:
        return body[i * 64 : (i + 1) * 64]

    if sel == SEL_LAUNCH_PLAIN:
        return [], "exact"
    if sel in (SEL_LAUNCH_EXEMPT, SEL_LAUNCH_FOR):
        arr_slot = 3 if sel == SEL_LAUNCH_EXEMPT else 4
        try:
            offset = int(word(arr_slot), 16) // 32
            count = int(word(offset), 16)
            return (["0x" + word(offset + 1 + i)[-40:]
                     for i in range(min(count, 64))], "exact")
        except (ValueError, IndexError):
            return [], "opaque"
    found = _heuristic_address_array(body)
    return (found, "heuristic") if found else ([], "opaque")


def probe_calldata(data: str) -> dict:
    """Structural analysis of a launch tx's calldata for unrecognized
    entrypoints: selector, size, and every length-prefixed address array with
    its word offset. Enough to derive an exact decoder from one sample."""
    body = data[10:] if data.startswith("0x") else data
    words = [body[i : i + 64] for i in range(0, len(body) - 63, 64)]
    arrays = []
    for i, w in enumerate(words):
        try:
            n = int(w, 16)
        except ValueError:
            continue
        if not (1 <= n <= 64):
            continue
        run = words[i + 1 : i + 1 + n]
        if len(run) == n and all(_looks_like_address_word(x) for x in run):
            arrays.append({"offset_word": i, "count": n,
                           "first": "0x" + run[0][-40:],
                           "last": "0x" + run[-1][-40:]})
    return {"selector": data[:10].lower(), "words": len(words),
            "head": words[:8], "address_arrays": arrays}


def block_near_time(rpc: EvmRpc, target_ts: int) -> int:
    """Approximate block at a unix timestamp via interpolation (Orbit chains
    have variable block times, so sample-and-refine instead of assuming a rate)."""
    hi = rpc.latest_block()
    hi_ts = rpc.get_block_time(hi)
    lo = max(hi - 1, 0)
    guess = hi
    for _ in range(8):
        if hi_ts <= target_ts:
            return hi
        span = max(hi - 200_000, 0)
        span_ts = rpc.get_block_time(span)
        if span_ts >= target_ts or hi_ts == span_ts:
            return span
        rate = (hi - span) / (hi_ts - span_ts)  # blocks per second
        guess = span + int((target_ts - span_ts) * rate)
        guess = max(min(guess, hi), 0)
        guess_ts = rpc.get_block_time(guess)
        if abs(guess_ts - target_ts) < 30:
            return guess
        if guess_ts < target_ts:
            span = guess
        else:
            hi, hi_ts = guess, guess_ts
    return guess


def snipe_window_activity(rpc: EvmRpc, launch: PonsLaunch,
                          window_blocks: int = 300) -> None:
    """Classify CurveBuy events right after launch: tax==0 → declared/bundle
    wallet buying at the untaxed price; tax>0 → outsider paying the snipe tax."""
    logs = rpc.get_logs(launch.block, launch.block + window_blocks,
                        address=launch.curve_or_pool, topics=[TOPIC_CURVE_BUY])
    for log in logs:
        data = log.get("data", "0x")[2:]
        if len(data) < 4 * 64:
            continue
        quote_in = int(data[0:64], 16)
        tax = int(data[192:256], 16)
        buyer = _addr(log["topics"][1])
        if buyer not in launch.snipe_buyers:
            launch.snipe_buyers.append(buyer)
        if tax == 0:
            if buyer != launch.deployer:
                launch.exempt_buys += 1
                launch.exempt_buy_quote += quote_in
                if buyer not in launch.exempt_buyer_wallets:
                    launch.exempt_buyer_wallets.append(buyer)
        else:
            launch.taxed_buys += 1


def recent_launches(rpc: EvmRpc, from_block: int, to_block: int,
                    deep: bool = True, limit: int = 200,
                    snipe_window_blocks: int = 300,
                    entrypoint_sink: dict | None = None) -> list[PonsLaunch]:
    launches: list[PonsLaunch] = []

    for log in rpc.get_logs(from_block, to_block, address=V2_FACTORY,
                            topics=[TOPIC_V2_LAUNCHED]):
        launches.append(PonsLaunch(
            version=2,
            token=_addr(log["topics"][1]),
            curve_or_pool=_addr(log["topics"][2]),
            deployer=_addr(log["topics"][3]),
            block=int(log["blockNumber"], 16),
            tx_hash=log["transactionHash"],
        ))

    for log in rpc.get_logs(from_block, to_block, address=V1_FACTORY,
                            topics=[TOPIC_V1_LAUNCHED]):
        data = log.get("data", "0x")[2:]
        pool = "0x" + data[64:128][-40:] if len(data) >= 128 else "?"
        launches.append(PonsLaunch(
            version=1,
            token=_addr(log["topics"][1]),
            deployer=_addr(log["topics"][2]),
            curve_or_pool=pool,
            block=int(log["blockNumber"], 16),
            tx_hash=log["transactionHash"],
        ))

    launches.sort(key=lambda l: -l.block)
    total_found = len(launches)
    launches = launches[:limit]
    if not deep:
        return launches

    print(f"found {total_found} launches; deep-analyzing the {len(launches)} "
          f"most recent (use --fast to skip, --limit to widen)...",
          file=sys.stderr)

    graduated_tokens = {
        _addr(log["topics"][1])
        for log in rpc.get_logs(from_block, rpc.latest_block(),
                                address=V2_FACTORY, topics=[TOPIC_GRADUATED])
    }

    txs = rpc.batch([("eth_getTransactionByHash", [l.tx_hash]) for l in launches])
    symbols = rpc.batch([("eth_call", [{"to": l.token, "data": SEL_SYMBOL}, "latest"])
                         for l in launches])
    unknown_entrypoints: dict[tuple[str, str], list] = {}
    for i, (launch, tx, sym) in enumerate(zip(launches, txs, symbols), 1):
        if isinstance(sym, str):
            launch.symbol = _decode_string(sym)
        launch.graduated = launch.token in graduated_tokens
        if launch.version == 2 and isinstance(tx, dict):
            calldata = tx.get("input", "")
            to = (tx.get("to") or "?").lower()
            sel = calldata[:10].lower()
            launch.declared_exemptions, launch.exemption_source = \
                exemptions_from_calldata(calldata)
            # a confirmed launch entrypoint isn't "unrecognized" — only flag
            # genuinely unknown ones for probing
            if launch.exemption_source not in ("exact", "corroborated") \
                    and not is_known_entrypoint(to, sel):
                entry = unknown_entrypoints.setdefault((to, sel),
                                                       [0, launch.tx_hash])
                entry[0] += 1
            try:
                snipe_window_activity(rpc, launch,
                                      window_blocks=snipe_window_blocks)
            except Exception as exc:
                print(f"note: snipe-window analysis failed for {launch.token} "
                      f"({exc}); continuing", file=sys.stderr)
            # heuristic calldata list vs execution ground truth: an extracted
            # wallet that actually bought tax-free proves the list is real
            if (launch.exemption_source == "heuristic"
                    and set(launch.declared_exemptions)
                    & set(launch.exempt_buyer_wallets)):
                launch.exemption_source = "corroborated"
        if i % 10 == 0 or i == len(launches):
            print(f"  analyzed {i}/{len(launches)}", file=sys.stderr)

    for (to, sel), (count, sample) in unknown_entrypoints.items():
        print(f"note: {count} launch(es) via unrecognized entrypoint {to} "
              f"selector {sel} (sample tx {sample}) — exemption lists for these "
              "are heuristic or opaque; report this line to add exact support.",
              file=sys.stderr)
        if entrypoint_sink is not None:
            entrypoint_sink[f"{to} {sel}"] = {"entrypoint": to, "selector": sel,
                                              "count": count, "sample_tx": sample}
    return launches


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch Pons launches on Robinhood Chain")
    ap.add_argument("--hours", type=float, default=1, help="lookback window")
    ap.add_argument("--limit", type=int, default=50,
                    help="max launches to deep-analyze, newest first")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fast", action="store_true",
                    help="skip per-launch calldata/curve analysis")
    args = ap.parse_args()

    rpc = EvmRpc()
    to_block = rpc.latest_block()
    print("locating start block...", file=sys.stderr)
    from_block = block_near_time(rpc, int(time.time() - args.hours * 3600))
    print(f"scanning blocks {from_block}-{to_block} for Pons launches...",
          file=sys.stderr)
    # Cover the ~15s snipe-tax window plus margin, in measured chain blocks.
    snipe_window = rpc.blocks_for_seconds(30, floor=100, cap=2_000)
    launches = recent_launches(rpc, from_block, to_block,
                               deep=not args.fast, limit=args.limit,
                               snipe_window_blocks=snipe_window)

    if args.json:
        print(json.dumps([asdict(l) for l in launches], indent=2))
        return

    print(f"\n{len(launches)} Pons launches in blocks {from_block}–{to_block} "
          f"(~{args.hours}h)\n")
    for l in launches:
        parts = [f"v{l.version}", l.symbol.ljust(10)[:10], l.token,
                 f"curve {l.curve_or_pool}", f"blk {l.block}"]
        if l.version == 2:
            if l.declared_exemptions:
                tag = "declared" if l.exemption_source == "exact" else "candidate"
                parts.append(f"BUNDLED: {len(l.declared_exemptions)} {tag} "
                             "exempt wallets")
            elif l.exemption_source == "opaque":
                parts.append("exemptions: opaque entrypoint")
            if l.exempt_buys:
                parts.append(f"{l.exempt_buys} tax-free snipe buys "
                             f"({l.exempt_buy_quote / 1e18:.3f} quote)")
            if l.taxed_buys:
                parts.append(f"{l.taxed_buys} taxed outside buys")
        if l.graduated:
            parts.append("GRADUATED")
        print("  ".join(parts))
    print("\nDeep-check any token: python -m src.onchain.report <token> "
          "--pair <its curve address above>")


if __name__ == "__main__":
    main()
