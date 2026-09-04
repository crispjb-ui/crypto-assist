"""Derive a launchpad's factory contract and event signatures from one of its
launched tokens — for launchpads with no published contracts (e.g. Long).

    python -m src.onchain.derive_factory 0xTOKEN

Method: binary-search the token's deploy block, find the transaction in that
block whose receipt contains the token's first events (the launch tx), then
report the entrypoint contract, the function selector called, and every
(contract, topic0) pair the launch emitted. Those constants are exactly what a
pons.py-style watcher for that launchpad needs.
"""
from __future__ import annotations

import argparse
import json

from . import explorer
from .rpc import EvmRpc, RpcError


def find_launch_tx(rpc: EvmRpc, token: str) -> tuple[dict, dict] | None:
    """Return (tx, receipt) of the transaction that deployed/launched the token."""
    token = token.lower()

    # Preferred: explorer knows the creation tx directly.
    creation = explorer._get({"module": "contract", "action": "getcontractcreation",
                              "contractaddresses": token})
    if isinstance(creation, list) and creation and isinstance(creation[0], dict):
        txh = creation[0].get("txHash") or creation[0].get("txhash")
        if txh:
            tx = rpc.call("eth_getTransactionByHash", [txh])
            rcpt = rpc.call("eth_getTransactionReceipt", [txh])
            if tx and rcpt:
                return tx, rcpt

    # Fallback: pure RPC. Deploy block, then the receipt whose logs the token emitted.
    deploy_block = rpc.find_deploy_block(token)
    block = rpc.call("eth_getBlockByNumber", [hex(deploy_block), True])
    txs = block.get("transactions") or []
    receipts = rpc.batch([("eth_getTransactionReceipt", [t["hash"]]) for t in txs])
    for tx, rcpt in zip(txs, receipts):
        if not isinstance(rcpt, dict):
            continue
        emitted = {log["address"].lower() for log in rcpt.get("logs") or []}
        created = (rcpt.get("contractAddress") or "").lower()
        if token in emitted or created == token:
            return tx, rcpt
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive launchpad factory from a token")
    ap.add_argument("token", help="a token launched by the target launchpad")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rpc = EvmRpc()
    try:
        found = find_launch_tx(rpc, args.token)
    except RpcError as exc:
        raise SystemExit(f"RPC error: {exc}")
    if not found:
        raise SystemExit(
            "Launch tx not found. The token may have been deployed directly "
            "(no factory), or historical state is unavailable on this RPC."
        )
    tx, rcpt = found

    entry = (tx.get("to") or "").lower() or "(contract creation)"
    selector = (tx.get("input") or "0x")[:10]
    emitters: dict[str, list[str]] = {}
    for log in rcpt.get("logs") or []:
        addr = log["address"].lower()
        topic0 = (log.get("topics") or ["0x?"])[0]
        emitters.setdefault(addr, [])
        if topic0 not in emitters[addr]:
            emitters[addr].append(topic0)

    result = {
        "token": args.token.lower(),
        "launch_tx": tx.get("hash"),
        "block": int(tx.get("blockNumber", "0x0"), 16),
        "deployer": (tx.get("from") or "").lower(),
        "entrypoint": entry,          # probable factory or its router
        "function_selector": selector,
        "event_emitters": emitters,   # contract -> [topic0, ...]
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"\ntoken:      {result['token']}")
    print(f"launch tx:  {result['launch_tx']}  (block {result['block']})")
    print(f"deployer:   {result['deployer']}")
    print(f"entrypoint: {entry}   selector {selector}")
    print("\nevents emitted in the launch tx (contract -> topic0):")
    for addr, topics in emitters.items():
        role = " <- the launched token" if addr == args.token.lower() else ""
        print(f"  {addr}{role}")
        for t in topics:
            print(f"      {t}")
    print(
        "\nThe entrypoint (or the non-token contract emitting a unique launch "
        "event) is the factory. Verify it on Blockscout — if its source is "
        "verified, the event names are readable there. Feed these constants "
        "into a watcher module like src/onchain/pons.py."
    )


if __name__ == "__main__":
    main()
