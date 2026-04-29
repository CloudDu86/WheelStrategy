"""
download_from_qc.py — Download files from QuantConnect Object Store via REST API

Prerequisites:
    pip install requests

Credentials (get from https://www.quantconnect.com/account):
    USER_ID   : numeric ID shown on your Account page
    API_TOKEN : generate from Account → API Access Token
"""

import hashlib
import hmac
import time
import requests
import json
from pathlib import Path

# ── Fill in your credentials ──────────────────────────────────────────────────
USER_ID   = "YOUR_USER_ID"    # e.g. "123456"
API_TOKEN = "YOUR_API_TOKEN"  # long alphanumeric string

OUTPUT_DIR = Path(".")         # save CSVs here

# ── Files to download ─────────────────────────────────────────────────────────
KEYS = [
    "options_chain_QQQ_TQQQ_combined.csv",
    "options_chain_QQQ.csv",
    "options_chain_TQQQ.csv",
]

# ── Auth helper ───────────────────────────────────────────────────────────────
def make_auth(user_id: str, api_token: str):
    """
    QuantConnect uses HTTP Basic Auth:
      username = user_id
      password = SHA256( api_token + ":" + timestamp ) + ":" + timestamp
    """
    timestamp = str(int(time.time()))
    password_raw = f"{api_token}:{timestamp}"
    password_hash = hashlib.sha256(password_raw.encode("utf-8")).hexdigest()
    password = f"{password_hash}:{timestamp}"
    return (user_id, password)


# ── Download each file ────────────────────────────────────────────────────────
def download_object(key: str) -> str | None:
    url    = "https://www.quantconnect.com/api/v2/object/read"
    params = {"key": key}
    auth   = make_auth(USER_ID, API_TOKEN)

    print(f"  GET {key} …", end=" ", flush=True)
    resp = requests.get(url, params=params, auth=auth, timeout=30)

    if resp.status_code == 200:
        try:
            # QC wraps response in JSON for some endpoints
            payload = resp.json()
            if isinstance(payload, dict) and "objectData" in payload:
                return payload["objectData"]
        except Exception:
            pass
        # Plain text / CSV response
        return resp.text
    else:
        print(f"FAILED ({resp.status_code}): {resp.text[:200]}")
        return None


def main():
    if USER_ID == "YOUR_USER_ID":
        print(
            "\n[ERROR] Please set USER_ID and API_TOKEN at the top of this script.\n"
            "  USER_ID   → https://www.quantconnect.com/account  (numeric)\n"
            "  API_TOKEN → Account → API Access Token\n"
        )
        return

    print(f"\nQuantConnect Object Store Downloader")
    print(f"User: {USER_ID}\n")

    for key in KEYS:
        content = download_object(key)
        if content is None:
            continue

        out_path = OUTPUT_DIR / key
        out_path.write_text(content, encoding="utf-8")

        lines = content.strip().split("\n")
        print(f"OK  →  {out_path}  ({len(lines)-1:,} rows)")

        # Preview first 3 data rows
        for line in lines[:4]:
            print(f"    {line}")
        if len(lines) > 4:
            print(f"    ... ({len(lines)-4} more rows)")
        print()

    print("Done.")


if __name__ == "__main__":
    main()
