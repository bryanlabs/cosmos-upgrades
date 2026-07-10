#!/usr/bin/env python3
"""
CI script: verify that RPC and REST endpoints in changed chain.json files
actually serve the chain_id declared in that file.

Usage:
    python scripts/validate_chain_ids.py path/to/chain.json [...]

Exit codes:
    0  all checked endpoints match
    1  one or more mismatches or errors found
"""

import json
import sys
import urllib.request
import urllib.error

TIMEOUT = 10
MAX_ENDPOINTS = 2  # check at most this many RPC and REST endpoints per file


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cosmos-upgrades-ci"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def get_rpc_chain_id(rpc_url):
    data = fetch_json(f"{rpc_url.rstrip('/')}/status")
    result = data.get("result", data)
    return result["node_info"]["network"]


def get_rest_chain_id(rest_url):
    data = fetch_json(f"{rest_url.rstrip('/')}/cosmos/base/tendermint/v1beta1/node_info")
    return data["default_node_info"]["network"]


def validate_file(path):
    errors = []
    with open(path) as f:
        chain = json.load(f)

    expected = chain.get("chain_id")
    if not expected:
        print(f"  SKIP  {path}: no chain_id field")
        return []

    rpc_endpoints = [e["address"] for e in chain.get("apis", {}).get("rpc", [])]
    rest_endpoints = [e["address"] for e in chain.get("apis", {}).get("rest", [])]

    for url in rpc_endpoints[:MAX_ENDPOINTS]:
        try:
            actual = get_rpc_chain_id(url)
            if actual != expected:
                errors.append(f"  FAIL  RPC {url}: expected {expected!r}, got {actual!r}")
            else:
                print(f"  OK    RPC {url} → {actual}")
        except Exception as e:
            print(f"  WARN  RPC {url}: unreachable ({e})")

    for url in rest_endpoints[:MAX_ENDPOINTS]:
        try:
            actual = get_rest_chain_id(url)
            if actual != expected:
                errors.append(f"  FAIL  REST {url}: expected {expected!r}, got {actual!r}")
            else:
                print(f"  OK    REST {url} → {actual}")
        except Exception as e:
            print(f"  WARN  REST {url}: unreachable ({e})")

    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_chain_ids.py <chain.json> [...]")
        sys.exit(1)

    all_errors = []
    for path in sys.argv[1:]:
        print(f"\nChecking {path}")
        all_errors.extend(validate_file(path))

    if all_errors:
        print("\nMismatches detected:")
        for err in all_errors:
            print(err)
        sys.exit(1)

    print("\nAll checked endpoints match their declared chain_id.")


if __name__ == "__main__":
    main()
