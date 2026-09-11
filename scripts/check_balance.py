"""Show the USDC and ETH balance of one or more addresses on Base mainnet.

    /opt/projectstate/venv/bin/python scripts/check_balance.py [0xADDRESS ...]

With no arguments it checks the server's own receiving wallet (X402_PAY_TO from .env).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_file = Path(__file__).resolve().parents[1] / ".env"
try:  # only needed for the default address; .env is readable by the service user only
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
except OSError:
    pass

from web3 import Web3  # noqa: E402

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base mainnet
ABI = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]}]

addresses = sys.argv[1:] or [a for a in [os.environ.get("X402_PAY_TO")] if a]
if not addresses:
    print("usage: check_balance.py 0xADDRESS [...]")
    raise SystemExit(1)

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 30}))
print(f"Base mainnet, block {w3.eth.block_number}\n")
for addr in addresses:
    try:
        a = Web3.to_checksum_address(addr)
    except Exception:
        print(f"{addr}  not a valid address")
        continue
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI).functions.balanceOf(a).call() / 1e6
    eth = w3.eth.get_balance(a) / 1e18
    print(f"{a}\n  USDC {usdc:.6f}   ETH {eth:.6f}   received/sent tx: {w3.eth.get_transaction_count(a)}")
    print(f"  explorer: https://basescan.org/address/{a}\n")
