"""
Tonnel marketplace integration, via the community `tonnelmp` package
(unofficial, reverse-engineered -- there is no official Tonnel API)
plus one hand-captured endpoint (`buyOffer/create`) that isn't covered
by tonnelmp at all -- see the offer functions below.

    pip install tonnelmp

Auth: needs an authData string captured from your own logged-in Tonnel
mini-app session (DevTools technique, see README). Set it as:

    export TONNEL_AUTH_DATA="..."

IMPORTANT: Tonnel's raw `price` field on regular listings does NOT
include their platform fee (~10%, added on top at purchase time per
tonnelmp's own docs) -- so the amount you'd actually pay is
`price * 1.10`. get_cheap_listings() applies that multiplier
automatically. buy_gift(), however, takes the RAW listed price (per
tonnelmp's own docs: "price - raw price, you dont have to multiply it
by 1.1") -- don't double-apply the fee there.

OFFER SUPPORT: tonnelmp doesn't expose an "offer" feature, but Tonnel's
own web app does have one (a Buy / Offer choice per listing) -- it's
just not part of the third-party library. All three functions below
are DevTools-confirmed, on two different hosts:

    POST https://gifts.coffin.meme/api/buyOffer/create
    {"amount": <TON>, "asset": "TON", "authData": "...", "gift_id": <int>}
    -> confirmed successful response: {"status": "success", "message": "success"}
       -- notably, NO offer id anywhere in it.

    POST https://gifts.coffin.meme/api/buyOffer/cancel
    {"authData": "...", "offer_id": <str>}
    -- takes `offer_id` (a short code like "0UFP6M37"), NOT `gift_id`.

    POST https://gifts2.tonnel.network/api/buyOffer/getMyOffers
    {"authData": "...", "filter": {}, "pageSize": 50, "tillTime": "<ISO datetime>"}
    -> {"status": "success", "offers": [{"offer_id": ..., "gift_id": ...,
        "gift_name": ..., "price": ..., "status": "pending"/"rejected"/
        "canceled"/..., "createdAt": "<ISO datetime>", ...}, ...]}

Since create_offer() never returns an offer id, list_my_offers() is
what sniper.py actually uses to find one after the fact (matching by
gift_id, or just processing every currently-pending offer) -- not
"tillTime": that appears to be a pagination cursor, not a filter start
time; we always pass "now" and take the first page.
"""

import logging
import os
from datetime import datetime, timezone

import httpx
from tonnelmp import buyGift, getAuctions, getGifts, info, placeBid

logger = logging.getLogger(__name__)

AUTH_DATA = os.environ.get("TONNEL_AUTH_DATA", "")
MARKET_NAME = "Tonnel"
TONNEL_FEE_MULTIPLIER = 1.10  # Tonnel adds ~10% on top of the listed price at purchase

OFFER_API_URL = "https://gifts.coffin.meme/api/buyOffer/create"
OFFER_CANCEL_API_URL = "https://gifts.coffin.meme/api/buyOffer/cancel"
MY_OFFERS_API_URL = "https://gifts2.tonnel.network/api/buyOffer/getMyOffers"

logger.info(
    "tonnel_market loaded -- TONNEL_AUTH_DATA is %s",
    f"set ({len(AUTH_DATA)} chars, starts {AUTH_DATA[:12]!r})" if AUTH_DATA else "NOT set",
)


def _require_auth() -> None:
    if not AUTH_DATA:
        raise RuntimeError(
            "Set TONNEL_AUTH_DATA (a token captured from your logged-in "
            "Tonnel mini-app session -- see README)."
        )


def get_cheap_listings(gift_name: str, max_ton: float = 10.0, limit: int = 30) -> list[dict]:
    """Returns current Tonnel listings for `gift_name` priced (fee-inclusive)
    at or below `max_ton`, cheapest first.

    Note: Tonnel's API rejects any `limit` over 30 with a 400 error, so
    it's capped here regardless of what's passed in.
    """
    _require_auth()
    limit = min(limit, 30)
    results = getGifts(
        gift_name=gift_name,
        limit=limit,
        sort="price_asc",
        asset="TON",
        price_range=[0, max_ton],
        authData=AUTH_DATA,
    )

    listings = []
    for item in results or []:
        raw_price = item.get("price")
        if raw_price is None:
            continue
        true_price = float(raw_price) * TONNEL_FEE_MULTIPLIER
        if true_price <= max_ton:
            listings.append(
                {
                    "market": MARKET_NAME,
                    "gift_name": item.get("name", gift_name),
                    "price_ton": true_price,
                    "id": item.get("gift_id"),
                }
            )
    return listings


def get_new_listings(limit: int = 30) -> list[dict]:
    """
    Returns the most recently listed gifts on Tonnel (newest first),
    raw (not fee-adjusted) price included. Pair with a `seen_ids` set
    in the caller to detect which ones are actually new since last poll.
    """
    _require_auth()
    limit = min(limit, 30)
    results = getGifts(limit=limit, sort="latest", asset="TON", authData=AUTH_DATA)
    listings = []
    for item in results or []:
        raw_price = item.get("price")
        gift_id = item.get("gift_id")
        if raw_price is None or gift_id is None:
            continue
        listings.append(
            {
                "gift_id": gift_id,
                "gift_name": item.get("name"),
                "gift_num": _parse_gift_num(item),
                "raw_price_ton": float(raw_price),
                "fee_inclusive_price_ton": float(raw_price) * TONNEL_FEE_MULTIPLIER,
                "can_transfer_since_ts": _parse_can_transfer_since(item),
            }
        )
    return listings


_GIFT_NUM_FIELD_CANDIDATES = ("gift_num", "num", "number", "giftNum")


def _parse_gift_num(item: dict) -> int | None:
    """
    Best-effort extraction of the gift's collection number (the "-1234"
    in t.me/nft/Name-1234 links) -- NOT the same as gift_id, which is
    Tonnel's own internal listing id. UNCONFIRMED field name -- tonnelmp
    accepts `gift_num` as a filter param on both getGifts()/getAuctions(),
    which is a strong hint it's also the response key, but not verified.
    """
    for key in _GIFT_NUM_FIELD_CANDIDATES:
        value = item.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _parse_can_transfer_since(item: dict) -> float | None:
    """
    Returns the Unix timestamp after which this gift can be
    transferred/traded, or None if the field isn't present. Confirmed
    field name from a live auction response (`can_transfer_since`, ISO
    8601 string) -- a "trade ban"/cooldown some gifts are still under
    (e.g. recently minted/received ones), during which they can't be
    resold or moved even if bought/won -- worth skipping entirely
    rather than tying up money in something unsellable for a while.
    """
    value = item.get("can_transfer_since")
    if value is None:
        return None
    return _to_unix_ts(value)


_END_TIME_FIELD_CANDIDATES = (
    # auctionEndTime CONFIRMED via a live response -- it's nested under
    # item["auction"]["auctionEndTime"], as an ISO 8601 string (e.g.
    # "2026-07-23T11:47:34.006Z"), not a top-level numeric timestamp like
    # originally guessed. The rest are fallback guesses kept just in case.
    "auctionEndTime", "auction_end_time",
    "end_time", "endTime", "finish_time", "finishTime", "ends_at", "endsAt",
    "expires_at", "expiresAt", "end_date", "endDate", "auction_end", "auctionEnd",
)


def _to_unix_ts(value) -> float | None:
    """Accepts either an ISO 8601 string (Tonnel's actual format) or a
    numeric unix timestamp in seconds or milliseconds."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value / 1000 if value > 10**12 else value  # ms vs seconds heuristic


def _parse_auction_end_ts(item: dict) -> float | None:
    """
    Best-effort extraction of the auction's end time as a Unix timestamp
    (seconds). Confirmed live shape: item["auction"]["auctionEndTime"] as
    an ISO string -- checked first, nested dict included as a fallback
    source in case a future response puts it elsewhere.
    """
    sources = (item.get("auction") or {}, item)
    for source in sources:
        for key in _END_TIME_FIELD_CANDIDATES:
            value = source.get(key)
            if value is None:
                continue
            ts = _to_unix_ts(value)
            if ts is not None:
                return ts
    return None


def _parse_current_bid(item: dict) -> float | None:
    """
    Best-effort current bid -- confirmed live shape nests auction details
    under item["auction"] (startingBid, bidHistory, ...). No live example
    with an actual bid in bidHistory was seen yet, so the "someone has
    bid" field names below are still a guess; startingBid (used when
    bidHistory is empty, as in the confirmed example) is solid.
    """
    auction_obj = item.get("auction") or {}
    for key in ("currentBid", "current_bid", "highestBid"):
        value = auction_obj.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass

    bid_history = auction_obj.get("bidHistory") or []
    if bid_history and isinstance(bid_history[-1], dict):
        for key in ("amount", "bid", "value"):
            value = bid_history[-1].get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass

    for key in ("startingBid", "starting_bid"):
        value = auction_obj.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass

    value = item.get("current_bid") or item.get("starting_bid")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_new_auctions(limit: int = 30) -> list[dict]:
    """
    Returns the most recently created auctions on Tonnel (newest first).
    Field names are a best-effort guess based on getGifts()'s shape --
    not yet confirmed against a live auction response, so treat the
    exact keys here as provisional until tested. `ends_at_ts` in
    particular may come back None if none of the guessed field names
    matched -- `raw` is included so the actual shape can be inspected
    and this fixed.
    """
    _require_auth()
    limit = min(limit, 30)
    results = getAuctions(limit=limit, sort="latest", authData=AUTH_DATA)
    auctions = []
    for item in results or []:
        auction_id = item.get("auction_id") or item.get("id")
        if auction_id is None:
            continue
        auctions.append(
            {
                "auction_id": auction_id,
                "gift_id": item.get("gift_id"),
                "gift_name": item.get("name"),
                "gift_num": _parse_gift_num(item),
                "current_bid_ton": _parse_current_bid(item),
                "ends_at_ts": _parse_auction_end_ts(item),
                "can_transfer_since_ts": _parse_can_transfer_since(item),
                "raw": item,
            }
        )
    return auctions


def buy_gift(gift_id: int, price: float, receiver: int | None = None) -> dict:
    """
    Buys a gift at its current RAW listed price (do NOT pre-multiply by
    the 1.10 fee -- tonnelmp's own docs say the fee is handled
    automatically on their end for this call specifically).
    Real money movement -- see sniper.py for the dry-run safeguard this
    is normally called behind.
    """
    _require_auth()
    return buyGift(gift_id=gift_id, price=price, authData=AUTH_DATA, receiver=receiver)


def place_bid(auction_id: str, amount: float) -> dict:
    """Places a bid on a Tonnel auction. Real money movement -- see
    sniper.py for the dry-run safeguard this is normally called behind."""
    _require_auth()
    return placeBid(auction_id=auction_id, amount=amount, authData=AUTH_DATA, asset="TON")


def get_balance_ton() -> float | None:
    """
    Returns your Tonnel platform balance in TON, or None if the response
    didn't contain any of the field names checked below.

    NOTE: tonnelmp's own docs only say info() returns "a dictionary
    containing balances, memo etc." -- the exact key wasn't confirmed
    against a live response while building this, so this checks a few
    plausible field names defensively and logs the raw response if none
    match, so you can add the right key here once you see it.
    """
    _require_auth()
    data = info(authData=AUTH_DATA) or {}

    for key in ("balance", "ton_balance", "balance_ton", "tonBalance"):
        value = data.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass

    nested = data.get("balance")
    if isinstance(nested, dict):
        for key in ("ton", "TON", "amount"):
            value = nested.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass

    logger.warning("get_balance_ton(): no known balance field in response: %r", data)
    return None


def create_offer(gift_id: int, amount: float) -> dict:
    """
    Submits a below-asking offer on a Tonnel listing, via a hand-captured
    endpoint not covered by tonnelmp (see module docstring). Real money
    movement -- see sniper.py for the dry-run safeguard this is normally
    called behind.

    Returns the raw response dict either way -- success/failure shape
    hasn't been confirmed yet, so check response contents yourself the
    first few times rather than trusting any particular key blindly.
    """
    _require_auth()
    body = {
        "amount": amount,
        "asset": "TON",
        "authData": AUTH_DATA,
        "gift_id": gift_id,
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(OFFER_API_URL, json=body)
        resp.raise_for_status()
        return resp.json()


def cancel_offer(offer_id: str) -> dict:
    """
    Cancels a previously-submitted offer on a Tonnel listing.
    DevTools-confirmed request shape -- see module docstring. Takes the
    offer's own id (a short alphanumeric code), not the gift's id.
    """
    _require_auth()
    body = {
        "authData": AUTH_DATA,
        "offer_id": offer_id,
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(OFFER_CANCEL_API_URL, json=body)
        resp.raise_for_status()
        return resp.json()


def _fetch_offers_page(till_time: str, page_size: int) -> list[dict]:
    """
    One page of getMyOffers -- see list_my_offers() for pagination.

    Unlike create_offer()/cancel_offer() (on gifts.coffin.meme, plain
    httpx is fine there), this host -- gifts2.tonnel.network -- sits
    behind Cloudflare bot protection and 403s a plain httpx request with
    no browser-like TLS fingerprint. tonnelmp's own info() (same host,
    different endpoint) works around this with curl_cffi's browser
    impersonation and a full set of browser headers -- replicated here
    the same way rather than guessed at.
    """
    _require_auth()
    from curl_cffi import requests as curl_requests
    from fake_useragent import UserAgent

    body = {
        "authData": AUTH_DATA,
        "filter": {},
        "pageSize": page_size,
        "tillTime": till_time,
    }
    headers = {
        "authority": "gifts2.tonnel.network",
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://market.tonnel.network",
        "priority": "u=1, i",
        "referer": "https://market.tonnel.network/",
        "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": UserAgent().random,
    }
    resp = curl_requests.post(
        MY_OFFERS_API_URL, headers=headers, json=body, impersonate="chrome110", timeout=15
    )
    if resp.status_code in (403, 429):
        raise Exception(
            f"list_my_offers(): request failed with status {resp.status_code} "
            "(likely Cloudflare)"
        )
    resp.raise_for_status()
    data = resp.json()
    return data.get("offers", []) if isinstance(data, dict) else []


def list_my_offers(page_size: int = 50, max_pages: int = 6, lookback_hours: float = 24.0) -> list[dict]:
    """
    Returns your offers from the last `lookback_hours` (any status --
    pending/rejected/canceled/...), via a DevTools-confirmed endpoint.
    This is how an offer's real offer_id gets found after the fact,
    since create_offer()'s own response never includes one -- see
    module docstring for the confirmed shape.

    Paginates using `tillTime` as a cursor (each page's oldest
    `createdAt` becomes the next page's `tillTime`), stopping once
    either a page comes back short (end of history), max_pages is hit,
    or the oldest record seen is already older than lookback_hours --
    a single 50-record page isn't enough once there's more history than
    that (e.g. many past offers/attempts), which silently hid old
    *pending* offers past page 1 from ever being auto-cancelled.

    IMPORTANT: keep both caps modest -- gifts2.tonnel.network sits
    behind Cloudflare, and this got called frequently enough (every
    poll, unbounded pagination) to trip a 403 block that also took down
    getGifts()/getAuctions() on the same host/IP for a while. There's
    no need to look back further than an hour or so past
    SNIPE_OFFER_AUTO_CANCEL_HOURS anyway -- anything actionable is
    recent by definition.
    """
    till_time = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cutoff_ts = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    seen: dict[str, dict] = {}
    for _ in range(max_pages):
        page = _fetch_offers_page(till_time, page_size)
        if not page:
            break

        new_count = 0
        oldest_created = None
        for o in page:
            key = o.get("_id") or o.get("offer_id")
            if key not in seen:
                seen[key] = o
                new_count += 1
            created = o.get("createdAt")
            if created and (oldest_created is None or created < oldest_created):
                oldest_created = created

        if len(page) < page_size or new_count == 0 or oldest_created is None:
            break

        oldest_ts = _to_unix_ts(oldest_created)
        if oldest_ts is not None and oldest_ts < cutoff_ts:
            break
        till_time = oldest_created

    return list(seen.values())
