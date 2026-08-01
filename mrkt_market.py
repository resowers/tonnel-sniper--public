"""
MRKT marketplace data, via the community `amrkt` package (unofficial,
reverse-engineered -- there is no official MRKT API). Used here purely
as a FLOOR PRICE REFERENCE for the sniper (see sniper.py) -- we buy/bid
on Tonnel, not MRKT itself.

    pip install amrkt

Auth: unlike Portals/Tonnel, amrkt handles login automatically using
YOUR OWN Telegram account via Pyrogram -- it reuses the same
TG_API_ID / TG_API_HASH already set up for the official Telegram
lookup (stargift_market.py). On first run it'll prompt you
interactively for your phone number and login code, then save its own
local Pyrogram session file (separate from the Telethon one).

DEPLOYING SOMEWHERE WITHOUT AN INTERACTIVE TERMINAL (e.g. Railway): you
can't do that phone/code prompt there. Instead, run
`export_session_string.py` once locally (after you've already logged
in here and amrkt_session.session exists), which prints a portable
session string -- set that as an AMRKT_SESSION_STRING env var on the
host, and _ensure_session_file() below will materialize the actual
session file from it on first startup there, with no prompt needed.

Field names and units (nanoTON for request params, *_ton properties
for convenience) below are taken directly from amrkt's own README
(https://github.com/TheBrainAir/amrkt), not guessed.

IMPORTANT: this module keeps ONE MarketClient instance alive for the
whole process (see _get_client()) instead of creating a new one per
call. amrkt's own _get_new_token() opens a fresh Pyrogram connection to
the local session SQLite file every time a MarketClient has no cached
API token yet -- so calling `async with MarketClient(...)` fresh on
every lookup meant every single call reopened that file. When the
sniper's two polling loops (listings + auctions) both fired lookups
around the same time, two concurrent opens of the same SQLite file
caused "database is locked". Reusing one instance means only the
*first* call ever touches the session file; every call after that
reuses the cached token via a plain HTTPS check
(MarketClient._is_token_valid()), no local file access at all. The
lock below serializes calls anyway, mainly to avoid two callers racing
to do that first, token-less auth at once.
"""

import asyncio
import logging
import os
import time

try:
    from amrkt import MarketClient
    _AMRKT_AVAILABLE = True
except ImportError:
    _AMRKT_AVAILABLE = False

logger = logging.getLogger(__name__)

API_ID = int(os.environ.get("TG_API_ID", "0") or "0")
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_STRING = os.environ.get("AMRKT_SESSION_STRING", "")
MARKET_NAME = "MRKT"

logger.info(
    "mrkt_market loaded -- AMRKT_SESSION_STRING is %s",
    f"set ({len(SESSION_STRING)} chars)" if SESSION_STRING else "NOT set",
)

SESSION_NAME = "amrkt_session"
WORKDIR = os.path.dirname(__file__) or "."
SESSION_PATH = os.path.join(WORKDIR, f"{SESSION_NAME}.session")

FLOOR_CACHE_TTL_SECONDS = 300  # collection floor prices don't need refetching every second
_floor_cache: dict[str, tuple[float, float]] = {}  # name_lower -> (fetched_at, floor_price_ton)

_session_file_checked = False

_client: "MarketClient | None" = None  # persistent instance, created once
_client_lock = asyncio.Lock()  # serializes creation + every call into it


def _require_credentials() -> None:
    if not _AMRKT_AVAILABLE:
        raise RuntimeError(
            "The 'amrkt' package isn't installed (it needs a C++ compiler on "
            "Windows to build one of its dependencies -- see README). "
            "MRKT lookups are unavailable until it's installed."
        )
    if not API_ID or not API_HASH:
        raise RuntimeError(
            "Set TG_API_ID and TG_API_HASH env vars (get them for free at "
            "https://my.telegram.org) -- see README."
        )


async def _ensure_session_file() -> None:
    """
    On a host with no interactive terminal (e.g. a fresh Railway deploy),
    materialize a real on-disk Pyrogram session file from
    AMRKT_SESSION_STRING instead of relying on amrkt's own interactive
    login -- see export_session_string.py.

    NOTE: Client(session_string=...) forces Pyrogram's storage into
    in_memory mode internally regardless of what's passed for
    in_memory (see pyrogram.Client.__init__) -- so connecting a Client
    that way never actually writes anything to disk. This works around
    that by opening the string directly via MemoryStorage, reading its
    decoded fields back out, and copying them into a fresh FileStorage
    at the path amrkt's own MarketClient expects -- so its normal
    (interactive-login-free) file-based flow finds a valid session
    already there.

    amrkt pins an exact kurigram version (see requirements.txt), whose
    pyrogram.storage API is the older split MemoryStorage/FileStorage
    shape (no unified SQLiteStorage(in_memory=...), and no
    server_address/port fields) -- this is written against that shape
    specifically, not the newer unified one some other kurigram release
    might have.

    Always (re)writes when AMRKT_SESSION_STRING is set, even if a
    session file already exists -- a stale/incomplete file left behind
    by a previous failed login attempt would otherwise silently block
    this from ever running again.
    """
    global _session_file_checked
    if _session_file_checked:
        return
    _session_file_checked = True

    if not SESSION_STRING:
        logger.warning(
            "_ensure_session_file(): AMRKT_SESSION_STRING is not set in this "
            "process's environment -- skipping bootstrap. amrkt will fall back "
            "to its own interactive login, which fails with no TTY (e.g. on "
            "Railway) -- if that's what you're seeing, the variable isn't "
            "actually reaching this container (check it's on the right "
            "service/environment, and that this deploy started *after* it was "
            "added)."
        )
        return

    logger.info(
        "_ensure_session_file(): materializing session file at %s from "
        "AMRKT_SESSION_STRING (%d chars)", SESSION_PATH, len(SESSION_STRING),
    )

    from pathlib import Path

    from pyrogram.storage import FileStorage, MemoryStorage

    try:
        mem_storage = MemoryStorage(SESSION_NAME, session_string=SESSION_STRING)
        await mem_storage.open()
        fields = {
            "dc_id": await mem_storage.dc_id(),
            "api_id": await mem_storage.api_id(),
            "test_mode": await mem_storage.test_mode(),
            "auth_key": await mem_storage.auth_key(),
            "user_id": await mem_storage.user_id(),
            "is_bot": await mem_storage.is_bot(),
        }
        await mem_storage.close()
        logger.info(
            "_ensure_session_file(): decoded session string -- dc_id=%s user_id=%s "
            "is_bot=%s (auth_key present: %s)",
            fields["dc_id"], fields["user_id"], fields["is_bot"], fields["auth_key"] is not None,
        )

        if os.path.exists(SESSION_PATH):
            os.remove(SESSION_PATH)

        file_storage = FileStorage(SESSION_NAME, Path(WORKDIR))
        await file_storage.open()
        await file_storage.dc_id(fields["dc_id"])
        await file_storage.api_id(fields["api_id"])
        await file_storage.test_mode(fields["test_mode"])
        await file_storage.auth_key(fields["auth_key"])
        await file_storage.user_id(fields["user_id"])
        await file_storage.is_bot(fields["is_bot"])
        await file_storage.date(0)
        await file_storage.save()
        await file_storage.close()
    except Exception:
        logger.exception("_ensure_session_file(): failed to materialize session file")
        raise

    size = os.path.getsize(SESSION_PATH) if os.path.exists(SESSION_PATH) else -1
    logger.info("_ensure_session_file(): done, %s is now %d bytes", SESSION_PATH, size)


async def _get_client() -> "MarketClient":
    """Returns the single shared MarketClient, creating it on first use.
    Never recreated after that -- see the module docstring for why."""
    global _client
    if _client is None:
        _require_credentials()
        await _ensure_session_file()
        _client = MarketClient(
            api_id=API_ID, api_hash=API_HASH, session_name=SESSION_NAME, workdir=WORKDIR
        )
    return _client


async def get_floor_price(gift_name: str) -> float | None:
    """
    Returns the current floor price (in TON) for the collection whose
    title case-insensitively contains `gift_name`, or None if no match.
    Cached for a few minutes since floor prices don't move every second.
    """
    cache_key = gift_name.strip().lower()
    cached = _floor_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < FLOOR_CACHE_TTL_SECONDS:
        return cached[1]

    async with _client_lock:
        client = await _get_client()
        collections = await client.get_collections()

    name_lower = gift_name.strip().lower()
    for c in collections:
        title = getattr(c, "title", "") or ""
        name = getattr(c, "name", "") or ""
        if name_lower in title.lower() or name_lower in name.lower():
            floor = float(c.floor_price_ton)
            _floor_cache[cache_key] = (time.time(), floor)
            return floor
    return None


async def get_cheap_listings(gift_name: str, max_ton: float = 10.0, limit: int = 50) -> list[dict]:
    """Returns current MRKT listings for `gift_name` priced at or below
    `max_ton`, cheapest first. (Kept for reference/manual checks --
    the sniper itself only needs get_floor_price.)"""
    async with _client_lock:
        client = await _get_client()
        result = await client.search_gifts(
            count=limit,
            ordering="Price",
            low_to_high=True,
            collection_names=[gift_name],
            max_price=int(max_ton * 1_000_000_000),  # amrkt takes prices in nanoTON
        )

    listings = []
    for gift in getattr(result, "items", []) or []:
        price = getattr(gift, "sale_price_ton", None)
        if price is None:
            continue
        listings.append(
            {
                "market": MARKET_NAME,
                "gift_name": getattr(gift, "name", gift_name),
                "price_ton": float(price),
                "id": getattr(gift, "id", None),
            }
        )
    return listings
