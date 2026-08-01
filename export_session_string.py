"""
One-off local helper: exports your already-logged-in amrkt_session.session
as a portable string, so you don't have to ship the raw session file (or
do an interactive phone/code login) when deploying elsewhere, e.g. Railway.

Run this ONCE, locally, where amrkt_session.session already exists and is
logged in (if mrkt_market.py's floor lookups have worked for you without
prompting for a phone number, it's already logged in):

    $env:TG_API_ID = "..."
    $env:TG_API_HASH = "..."
    python export_session_string.py

Copy the printed string and set it as AMRKT_SESSION_STRING on your
deployment host's env vars (e.g. Railway) -- mrkt_market.py will use it
to recreate the session file there automatically on first startup, no
interactive login needed.

WARNING: this string grants full access to the Telegram account that
generated it (same trust level as amrkt_session.session itself). Treat
it like a password -- never commit it to git, never paste it anywhere
you don't control the storage of.
"""

import asyncio
import os

from pyrogram import Client

API_ID = int(os.environ.get("TG_API_ID", "0") or "0")
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = "amrkt_session"
WORKDIR = os.path.dirname(__file__) or "."


async def main() -> None:
    if not API_ID or not API_HASH:
        raise SystemExit("Set TG_API_ID and TG_API_HASH first.")

    session_path = os.path.join(WORKDIR, f"{SESSION_NAME}.session")
    if not os.path.exists(session_path):
        raise SystemExit(
            f"{session_path} doesn't exist yet -- run the sniper/smoke_test "
            "locally first so amrkt can log in interactively and create it."
        )

    async with Client(
        SESSION_NAME, api_id=API_ID, api_hash=API_HASH, workdir=WORKDIR
    ) as client:
        session_string = await client.export_session_string()

    print("\nYour portable session string (treat this like a password):\n")
    print(session_string)
    print("\nSet this as AMRKT_SESSION_STRING on your deployment host.")


if __name__ == "__main__":
    asyncio.run(main())
