"""
Simulates a completely fresh deploy (no local amrkt_session.session) to
verify AMRKT_SESSION_STRING actually works end-to-end -- catches
problems here, locally, instead of burning a Railway build/deploy cycle
per attempt.

Usage (uses a real MRKT floor lookup, so needs real credentials):
    $env:TG_API_ID = "..."
    $env:TG_API_HASH = "..."
    $env:AMRKT_SESSION_STRING = "..."   # from export_session_string.py
    python test_session_bootstrap.py
"""

import asyncio
import os
import tempfile

import mrkt_market

if not os.environ.get("AMRKT_SESSION_STRING"):
    raise SystemExit("Set AMRKT_SESSION_STRING first (see export_session_string.py).")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as fake_workdir:
        # Point the module at an empty directory, as if this were a fresh
        # Railway container with no session file on disk yet.
        mrkt_market.WORKDIR = fake_workdir
        mrkt_market.SESSION_PATH = os.path.join(fake_workdir, f"{mrkt_market.SESSION_NAME}.session")

        print(f"Simulating a fresh host with workdir={fake_workdir} (no session file present)\n")

        floor = await mrkt_market.get_floor_price("Desk Calendar")
        print(f"get_floor_price result: {floor}")

        if os.path.exists(mrkt_market.SESSION_PATH):
            size = os.path.getsize(mrkt_market.SESSION_PATH)
            print(f"\nOK: session file materialized on disk ({size} bytes) from AMRKT_SESSION_STRING.")
        else:
            print("\nFAILED: no session file was written -- bootstrap didn't run as expected.")


if __name__ == "__main__":
    asyncio.run(main())
