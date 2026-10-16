#!/usr/bin/env python3
"""
Decode every transaction that touched a Solana wallet and flag any that call
a program outside a known-safe list.

Usage:
    export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
    python3 decode_fee_wallet.py AD3CFWfpi4dwnnp6JFsWvwM5oXX3atQABtjUe3BCCUaL
    python3 decode_fee_wallet.py <address> --csv out.csv

Needs: Python 3.8+, `pip install requests`.
Any Solana RPC works; the public endpoint (https://api.mainnet-beta.solana.com)
is heavily rate-limited, so a provider key is recommended. Never commit the key.
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

import requests

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Programs a normal staking site would use. Anything else gets flagged.
KNOWN_PROGRAMS = {
    "11111111111111111111111111111111": "System",
    "Stake11111111111111111111111111111111111111": "Stake",
    "ComputeBudget111111111111111111111111111111": "ComputeBudget",
    "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95": "Lighthouse (Phantom)",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo",
    "SysvarC1ock11111111111111111111111111111111": "Clock sysvar",
}


def rpc(method, params, retries=6):
    """Call the RPC, backing off on rate limits."""
    for attempt in range(retries):
        r = requests.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=60,
        )
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]
    raise RuntimeError(f"{method}: rate-limited after {retries} attempts")


def all_signatures(address):
    """Every signature that touched the address, oldest first."""
    sigs, before = [], None
    while True:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [address, opts])
        sigs.extend(page)
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    return list(reversed(sigs))


def describe(ix):
    """One readable line per instruction."""
    prog = ix.get("programId", "")
    name = KNOWN_PROGRAMS.get(prog, prog)
    parsed = ix.get("parsed")
    if isinstance(parsed, dict):
        info = parsed.get("info", {})
        bits = [f"{name}.{parsed.get('type')}"]
        if "lamports" in info:
            bits.append(f"{int(info['lamports']) / 1e9:.9f} SOL")
        for key in ("stakeAccount", "newAccount", "destination", "source"):
            if key in info:
                bits.append(f"{key}={info[key]}")
        return " ".join(bits)
    return f"{name} (unparsed)"


def decode(address):
    rows = []
    sigs = all_signatures(address)
    print(f"{len(sigs)} transactions found for {address}\n", file=sys.stderr)

    for i, s in enumerate(sigs, 1):
        tx = rpc(
            "getTransaction",
            [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if tx is None:
            continue
        msg = tx["transaction"]["message"]
        keys = [k["pubkey"] for k in msg["accountKeys"]]
        signers = [k["pubkey"] for k in msg["accountKeys"] if k.get("signer")]

        # Outer instructions plus the inner ones programs call on the user's behalf.
        instructions = list(msg["instructions"])
        for inner in tx["meta"].get("innerInstructions") or []:
            instructions.extend(inner["instructions"])

        programs = {ix.get("programId") for ix in instructions}
        unknown = sorted(p for p in programs if p not in KNOWN_PROGRAMS)

        # SOL change for the address itself (the fee it received, for a fee wallet).
        idx = keys.index(address) if address in keys else None
        delta = None
        if idx is not None:
            delta = (tx["meta"]["postBalances"][idx] - tx["meta"]["preBalances"][idx]) / 1e9

        rows.append({
            "n": i,
            "time_utc": datetime.fromtimestamp(tx["blockTime"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "signature": s["signature"],
            "succeeded": tx["meta"]["err"] is None,
            "signers": " ".join(signers),
            "sol_change_for_address": delta,
            "unknown_programs": " ".join(unknown),
            "instructions": " | ".join(describe(ix) for ix in instructions),
        })
        print(f"[{i}/{len(sigs)}] {rows[-1]['time_utc']} {s['signature'][:12]}... "
              f"{'FLAG ' + ','.join(unknown) if unknown else 'ok'}", file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address", help="wallet to decode, e.g. the site's fee wallet")
    ap.add_argument("--csv", help="write every decoded transaction to this CSV file")
    args = ap.parse_args()

    rows = decode(args.address)

    flagged = [r for r in rows if r["unknown_programs"]]
    print(f"\n{len(rows)} decoded; {len(flagged)} call a program outside the known list:")
    for r in flagged:
        print(f"  {r['time_utc']}  {r['signature']}\n    unknown: {r['unknown_programs']}\n    {r['instructions']}")

    signers = {}
    for r in rows:
        for s in r["signers"].split():
            signers[s] = signers.get(s, 0) + 1
    print("\nSigners (transactions each):")
    for s, n in sorted(signers.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {s}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["n"])
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
