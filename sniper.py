"""
Watches Tonnel for newly listed gifts and newly created auctions,
prices each one against its MRKT floor price, and automatically:

  - New fixed-price listings: submits a below-asking OFFER on Tonnel
    at `floor_price * (1 - discount%)` -- or, if the listing is
    *already* at or below that target, does an instant direct BUY
    instead (guaranteed, rather than an offer the seller might ignore).
  - New auctions: places a BID on Tonnel at the same target price.

SAFETY: this executes real financial transactions once enabled. It
runs in DRY-RUN mode by default -- every match is logged/alerted but
NOT actually submitted. Set SNIPER_LIVE_MODE=true to arm it for real.
Even in live mode, a running total is checked against
SNIPER_MAX_SESSION_SPEND_TON before every purchase/bid/offer, as a
backstop against bugs or unexpected repeat-fires.

Config (env vars):
    SNIPE_OFFER_DISCOUNT_PERCENT  (default: 15)   -- target discount off floor for listing
                                                       offers/buys, in %
    SNIPE_AUCTION_DISCOUNT_PERCENT (default: 10)  -- target discount off floor for auction
                                                       bids, in %
    SNIPE_MIN_DISCOUNT_TON        (default: 0.3)  -- floor price this discount, whichever
                                                       cuts deeper: target = floor -
                                                       max(floor * percent/100, this)
    SNIPER_MAX_GIFT_PRICE_TON     (default: 10)   -- skip any gift whose MRKT floor is
                                                       above this (caps every action's
                                                       spend, since target is always <= floor)
    SNIPE_MAX_PREMIUM_PERCENT     (default: 15)   -- ignore listings priced more than this %
                                                       above MRKT floor (e.g. a 3 TON floor +
                                                       15% = 3.45 TON ceiling -- anything
                                                       pricier is skipped, not offered).
                                                       Auctions are NOT subject to this.
    SNIPE_OFFER_AUTO_CANCEL_HOURS (default: 1)    -- a submitted offer that's still pending
                                                       after this long gets auto-cancelled --
                                                       BUT SEE README: Tonnel's create-offer
                                                       response doesn't return an offer_id, so
                                                       this currently can't actually cancel
                                                       anything -- it just stops tracking the
                                                       offer after this long, logging that it
                                                       couldn't be cancelled programmatically
    SNIPER_AUCTION_BID_WINDOW_HOURS (default: 3)  -- only bid on an auction once this many
                                                       hours or fewer remain before it ends
    SNIPER_BLACKLIST              (optional)      -- comma-separated collection names to
                                                       never act on, e.g. "Santa Hat,Jelly Bunny"
                                                       (case/space-insensitive match)
    SNIPER_MIN_BALANCE_TON        (default: 50)   -- refuse to buy/offer/bid if it would
                                                       drop your live Tonnel balance below
                                                       this (Tonnel reserves TON per pending
                                                       offer, and offers can't be
                                                       auto-cancelled yet -- see README)
    POLL_INTERVAL_SECONDS         (default: 20)   -- how often to re-check Tonnel
    SNIPER_LIVE_MODE              (default: false) -- "true" to actually execute
    SNIPER_MAX_SESSION_SPEND_TON  (default: 20)   -- hard cap on real TON spent this run
    TG_ALERT_BOT_TOKEN            (optional)      -- send alerts to yourself via Telegram
    TG_ALERT_CHAT_ID              (optional)      -- chat id to send alerts to

State (which listings/auctions have already been seen) persists in
sniper_state.json next to this script, so restarting doesn't re-alert
or re-buy the same items. Every action taken (dry-run or live) is also
appended to sniper_history.json via sniper_history.py, for later review.

Usage:
    python sniper.py
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import mrkt_market
import sniper_history
import tonnel_market

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

OFFER_DISCOUNT_PERCENT = float(os.environ.get("SNIPE_OFFER_DISCOUNT_PERCENT", "15"))
AUCTION_DISCOUNT_PERCENT = float(os.environ.get("SNIPE_AUCTION_DISCOUNT_PERCENT", "10"))
MIN_DISCOUNT_TON = float(os.environ.get("SNIPE_MIN_DISCOUNT_TON", "0.3"))
MAX_GIFT_PRICE_TON = float(os.environ.get("SNIPER_MAX_GIFT_PRICE_TON", "10"))
MAX_PREMIUM_PERCENT = float(os.environ.get("SNIPE_MAX_PREMIUM_PERCENT", "15"))
OFFER_AUTO_CANCEL_HOURS = float(os.environ.get("SNIPE_OFFER_AUTO_CANCEL_HOURS", "1"))
AUCTION_BID_WINDOW_HOURS = float(os.environ.get("SNIPER_AUCTION_BID_WINDOW_HOURS", "3"))
BLACKLIST = {
    name.strip().replace(" ", "").lower()
    for name in os.environ.get("SNIPER_BLACKLIST", "").split(",")
    if name.strip()
}
MIN_BALANCE_TON = float(os.environ.get("SNIPER_MIN_BALANCE_TON", "50"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "20"))
# Deliberately much slower than POLL_INTERVAL_SECONDS -- list_my_offers()
# hits a Cloudflare-protected host and paginates through offer history;
# polling it every 20s is what triggered a 403 block that also took down
# getGifts()/getAuctions() on the same host. An hour-scale cutoff doesn't
# need second-scale precision anyway.
OFFER_CHECK_INTERVAL_SECONDS = int(os.environ.get("SNIPER_OFFER_CHECK_INTERVAL_SECONDS", "180"))
# When a Tonnel call fails with what looks like a Cloudflare block, back
# off much longer than the normal retry interval instead of hammering it
# again in 20s -- retrying fast while a rate-limit/block is active is
# exactly what triggers or extends it.
CLOUDFLARE_BACKOFF_SECONDS = int(os.environ.get("SNIPER_CLOUDFLARE_BACKOFF_SECONDS", "120"))
LIVE_MODE = os.environ.get("SNIPER_LIVE_MODE", "false").strip().lower() == "true"
MAX_SESSION_SPEND_TON = float(os.environ.get("SNIPER_MAX_SESSION_SPEND_TON", "20"))

BALANCE_CACHE_TTL_SECONDS = 15

TG_ALERT_BOT_TOKEN = os.environ.get("TG_ALERT_BOT_TOKEN", "")
TG_ALERT_CHAT_ID = os.environ.get("TG_ALERT_CHAT_ID", "")

STATE_FILE = os.path.join(os.path.dirname(__file__), "sniper_state.json")

_session_spend_ton = 0.0


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        state = {}
    else:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    state.setdefault("seen_listings", [])
    state.setdefault("seen_auctions", [])
    state.setdefault("pending_auctions", [])  # [{auction_id, gift_id, gift_name, ends_at_ts}]
    return state


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


async def _alert(message: str) -> None:
    """Sends a Telegram alert if configured, always logs regardless."""
    logger.info(message)
    if not (TG_ALERT_BOT_TOKEN and TG_ALERT_CHAT_ID):
        return
    try:
        from telegram import Bot

        bot = Bot(token=TG_ALERT_BOT_TOKEN)
        await bot.send_message(chat_id=TG_ALERT_CHAT_ID, text=message)
    except Exception as e:
        logger.warning("Failed to send Telegram alert: %s", e)


def _target_price(floor_price: float, discount_percent: float) -> float:
    """
    target = floor - discount, where discount is whichever cuts deeper:
    discount_percent% of the floor, or the flat MIN_DISCOUNT_TON floor
    (e.g. on a cheap 1 TON gift, 10% is only 0.1 TON off -- too shallow
    to be worth chasing -- so the flat 0.3 TON minimum kicks in instead).
    Offers and auction bids use different percentages -- see
    OFFER_DISCOUNT_PERCENT / AUCTION_DISCOUNT_PERCENT.
    """
    discount = max(floor_price * discount_percent / 100, MIN_DISCOUNT_TON)
    return round(max(floor_price - discount, 0.0), 4)


def _price_ceiling(floor_price: float) -> float:
    """Listings priced above this (floor + MAX_PREMIUM_PERCENT%) are ignored
    outright -- not worth offering on something that far above floor."""
    return round(floor_price * (1 + MAX_PREMIUM_PERCENT / 100), 4)


def _is_blacklisted(gift_name: str) -> bool:
    return gift_name.strip().replace(" ", "").lower() in BLACKLIST


def _retry_delay(e: Exception, normal_seconds: int) -> int:
    """Longer backoff for what looks like a Cloudflare block/rate-limit
    (tonnelmp and tonnel_market.list_my_offers() both surface these as
    403s, sometimes with "CloudFlare" literally in the message) --
    retrying at the normal fast interval while blocked just keeps
    poking it and can prolong the block."""
    text = str(e)
    if "403" in text or "cloudflare" in text.lower():
        return CLOUDFLARE_BACKOFF_SECONDS
    return normal_seconds


_auth_error_alerted = False


async def _check_auth_error(result) -> None:
    """
    tonnelmp's getGifts()/getAuctions() label EVERY 403 "(Likely
    CloudFlare)" without looking at the response body, so a genuinely
    expired/invalid TONNEL_AUTH_DATA looks identical to a rate-limit
    block in those logs. buy_gift()/create_offer()/place_bid()/
    cancel_offer() do return a real JSON body though (via
    gifts.coffin.meme) -- if THAT says the auth itself is bad, it's
    unambiguous, so fire one loud, one-time alert instead of letting it
    blend into routine warnings. authData is short-lived by design (see
    README) and needs a fresh DevTools capture when this fires.
    """
    global _auth_error_alerted
    if _auth_error_alerted or not isinstance(result, dict):
        return
    message = str(result.get("message", "")).lower()
    if "auth" in message and ("invalid" in message or "expired" in message):
        _auth_error_alerted = True
        await _alert(
            "🔑 TONNEL_AUTH_DATA looks expired/invalid (Tonnel said: "
            f"{result.get('message')!r}). The sniper can't actually buy/"
            "offer/bid until you capture a fresh authData via DevTools and "
            "update it on Railway -- see README. (This alert only fires "
            "once per run.)"
        )


def _is_trade_banned(item: dict) -> bool:
    """True if this gift is still under Tonnel's transfer cooldown
    (can_transfer_since_ts in the future) -- can't be resold/moved yet
    even if bought/won, so not worth touching."""
    ts = item.get("can_transfer_since_ts")
    return ts is not None and ts > time.time()


def _nft_link(gift_name: str, gift_num: int | None, fallback_id) -> str:
    """
    Builds a t.me/nft/<Name><Number> link, e.g. https://t.me/nft/SurgeBoard-22116
    -- <Number> is the gift's collection number (gift_num), NOT Tonnel's own
    internal gift_id. Falls back to gift_id if gift_num couldn't be
    determined (see _parse_gift_num() in tonnel_market.py) -- the link
    just won't resolve to the right item in that case.
    """
    slug = (gift_name or "").replace(" ", "")
    number = gift_num if gift_num is not None else fallback_id
    return f"https://t.me/nft/{slug}-{number}"


_balance_cache: tuple[float, float | None] | None = None  # (fetched_at, balance_ton)


def _current_balance_ton() -> float | None:
    """Cached for BALANCE_CACHE_TTL_SECONDS so a burst of matches doesn't
    hammer the balance endpoint once per alert."""
    global _balance_cache
    now = time.time()
    if _balance_cache and (now - _balance_cache[0]) < BALANCE_CACHE_TTL_SECONDS:
        return _balance_cache[1]
    try:
        balance = tonnel_market.get_balance_ton()
    except Exception as e:
        logger.warning("Balance lookup failed: %s", e)
        balance = None
    _balance_cache = (now, balance)
    return balance


def _balance_str() -> str:
    balance = _current_balance_ton()
    return "unknown" if balance is None else f"{balance:.2f} TON"


def _spend_allowed(amount: float) -> bool:
    global _session_spend_ton
    if _session_spend_ton + amount > MAX_SESSION_SPEND_TON:
        logger.warning(
            "Skipping: %.2f TON would exceed the session spend cap "
            "(%.2f already spent, cap is %.2f). Raise "
            "SNIPER_MAX_SESSION_SPEND_TON if this is expected.",
            amount, _session_spend_ton, MAX_SESSION_SPEND_TON,
        )
        return False
    return True


def _balance_allows(amount: float) -> bool:
    """
    Live-balance floor check for buys/bids -- separate from
    _spend_allowed()'s per-session counter. Buys are immediate,
    irreversible spends and a winning bid can't be un-bid, so both are
    simply refused below the floor. Offers deliberately do NOT use this
    (see the offer branch in watch_new_listings()) -- those get let
    through regardless, and cancel_stale_offers() cancels the oldest
    pending offer to compensate if that pushes balance below the floor.
    """
    if MIN_BALANCE_TON <= 0:
        return True
    balance = _current_balance_ton()
    if balance is None:
        logger.warning(
            "Balance unknown -- can't verify the %.1f TON floor, allowing "
            "this action to proceed anyway.", MIN_BALANCE_TON,
        )
        return True
    if balance - amount < MIN_BALANCE_TON:
        logger.warning(
            "Skipping: %.2f TON balance minus this %.2f TON action would drop "
            "below the %.1f TON floor (SNIPER_MIN_BALANCE_TON). Cancel some "
            "pending offers in Tonnel directly, or raise the floor if this "
            "is expected.",
            balance, amount, MIN_BALANCE_TON,
        )
        return False
    return True


def _record_spend(amount: float) -> None:
    global _session_spend_ton
    _session_spend_ton += amount


def _offer_age_hours(offer: dict) -> float:
    try:
        created = datetime.fromisoformat(offer["createdAt"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except (KeyError, ValueError, TypeError):
        return 0.0


async def cancel_stale_offers(state: dict) -> None:
    """
    Fetches your real pending offers directly from Tonnel (via
    tonnel_market.list_my_offers()) every poll and cancels the oldest
    ones when either:
      - an offer has been pending longer than OFFER_AUTO_CANCEL_HOURS, or
      - live balance has dropped below SNIPER_MIN_BALANCE_TON (cancels
        oldest-first until back above it, or nothing left to cancel).

    Doesn't rely on capturing an offer_id ourselves at submission time --
    create_offer()'s response never includes one; Tonnel's own offer list
    does, keyed by its own createdAt, which is what "pending" actually
    means here regardless of what this process itself has seen.
    """
    while True:
        try:
            offers = tonnel_market.list_my_offers()
        except Exception as e:
            logger.warning("Fetching my offers failed: %s", e)
            await asyncio.sleep(_retry_delay(e, OFFER_CHECK_INTERVAL_SECONDS))
            continue

        pending = [o for o in offers if o.get("status") == "pending" and o.get("offer_id")]
        pending.sort(key=_offer_age_hours, reverse=True)  # oldest first

        to_cancel: dict[str, str] = {}  # offer_id -> reason

        for o in pending:
            age_hours = _offer_age_hours(o)
            if age_hours >= OFFER_AUTO_CANCEL_HOURS:
                to_cancel[o["offer_id"]] = (
                    f"pending {age_hours:.1f}h (limit {OFFER_AUTO_CANCEL_HOURS:.0f}h)"
                )

        if MIN_BALANCE_TON > 0:
            balance = _current_balance_ton()
            if balance is not None and balance < MIN_BALANCE_TON:
                for o in pending:
                    if balance >= MIN_BALANCE_TON:
                        break
                    if o["offer_id"] in to_cancel:
                        continue
                    to_cancel[o["offer_id"]] = (
                        f"balance {balance:.2f} TON is below the "
                        f"{MIN_BALANCE_TON:.0f} TON floor"
                    )
                    balance += float(o.get("price") or 0)  # what cancelling frees up

        for o in pending:
            offer_id = o.get("offer_id")
            if offer_id not in to_cancel:
                continue
            gift_name = o.get("gift_name", "")
            reason = to_cancel[offer_id]
            msg = (
                f"⏱ Offer for {gift_name} (offer {offer_id}): {reason} -- "
                + ("cancelling." if LIVE_MODE else "[DRY RUN] would cancel.")
            )
            await _alert(msg)
            if LIVE_MODE:
                try:
                    result = tonnel_market.cancel_offer(offer_id)
                    await _alert(f"Cancel result for {gift_name}: {result}")
                    await _check_auth_error(result)
                    sniper_history.add_record({
                        "type": "offer_cancelled", "gift_name": gift_name,
                        "gift_id": o.get("gift_id"), "offer_id": offer_id, "result": str(result),
                    })
                except Exception as e:
                    await _alert(f"Offer cancel FAILED for {gift_name}: {e}")
                    sniper_history.add_record({
                        "type": "offer_cancelled", "gift_name": gift_name,
                        "gift_id": o.get("gift_id"), "offer_id": offer_id, "result": f"FAILED: {e}",
                    })

        await asyncio.sleep(OFFER_CHECK_INTERVAL_SECONDS)


async def watch_new_listings(state: dict) -> None:
    seen = set(state["seen_listings"])
    if not seen:
        try:
            existing = tonnel_market.get_new_listings()
            seen.update(item["gift_id"] for item in existing)
            state["seen_listings"] = list(seen)
            _save_state(state)
            logger.info(
                "[LISTING] No prior state -- marked %d currently-listed gifts as "
                "already seen (won't act on the existing backlog, only on new "
                "listings from here on).", len(existing),
            )
        except Exception as e:
            logger.warning("Priming seen-listings failed: %s", e)

    while True:
        try:
            listings = tonnel_market.get_new_listings()
        except Exception as e:
            logger.warning("Tonnel new-listings check failed: %s", e)
            await asyncio.sleep(_retry_delay(e, POLL_INTERVAL_SECONDS))
            continue

        for item in listings:
            gift_id = item["gift_id"]
            if gift_id in seen:
                continue
            seen.add(gift_id)
            state["seen_listings"] = list(seen)
            _save_state(state)

            gift_name = item.get("gift_name") or ""
            if _is_blacklisted(gift_name):
                continue
            if _is_trade_banned(item):
                logger.info(
                    "[LISTING] %s (id %s): still under Tonnel's transfer cooldown "
                    "-- skipping.", gift_name, gift_id,
                )
                continue
            raw_price = item["raw_price_ton"]
            link = _nft_link(gift_name, item.get("gift_num"), gift_id)

            try:
                floor = await mrkt_market.get_floor_price(gift_name)
            except Exception as e:
                logger.warning("MRKT floor lookup failed for %r: %s", gift_name, e)
                continue
            if floor is None:
                continue  # no MRKT reference price for this collection, skip
            if floor > MAX_GIFT_PRICE_TON:
                logger.info(
                    "[LISTING] %s (id %s): floor %.2f TON is above the %.2f TON cap, skipping.",
                    gift_name, gift_id, floor, MAX_GIFT_PRICE_TON,
                )
                continue

            ceiling = _price_ceiling(floor)
            if raw_price > ceiling:
                logger.info(
                    "[LISTING] %s (id %s): asking %s TON is above the +%.0f%% ceiling "
                    "(%.2f TON, floor %.2f TON) -- ignoring.",
                    gift_name, gift_id, raw_price, MAX_PREMIUM_PERCENT, ceiling, floor,
                )
                continue

            target = _target_price(floor, OFFER_DISCOUNT_PERCENT)
            balance = _balance_str()

            if raw_price <= target:
                # Already cheap enough outright -- an instant buy beats an
                # offer the seller might just ignore.
                msg = (
                    f"Продажа\n"
                    f"{link}\n"
                    f"Цена: {raw_price} TON\n"
                    f"Таргет: {target} TON\n"
                    f"Действие: Покупка ({'LIVE' if LIVE_MODE else 'DRY RUN'})\n"
                    f"Баланс: {balance}"
                )
                await _alert(msg)
                sniper_history.add_record({
                    "type": "buy", "gift_name": gift_name, "gift_id": gift_id,
                    "action_price_ton": raw_price, "floor_price_ton": floor,
                    "balance_ton": balance, "mode": "live" if LIVE_MODE else "dry_run",
                    "result": None,
                })
                if LIVE_MODE and _spend_allowed(raw_price) and _balance_allows(raw_price):
                    try:
                        result = tonnel_market.buy_gift(gift_id, raw_price)
                        _record_spend(raw_price)
                        await _alert(f"Buy result for {gift_name}: {result}")
                        await _check_auth_error(result)
                        sniper_history.add_record({
                            "type": "buy_result", "gift_name": gift_name, "gift_id": gift_id,
                            "action_price_ton": raw_price, "result": str(result),
                        })
                    except Exception as e:
                        await _alert(f"Buy FAILED for {gift_name}: {e}")
                        sniper_history.add_record({
                            "type": "buy_result", "gift_name": gift_name, "gift_id": gift_id,
                            "action_price_ton": raw_price, "result": f"FAILED: {e}",
                        })
            else:
                msg = (
                    f"Продажа\n"
                    f"{link}\n"
                    f"Цена: {raw_price} TON\n"
                    f"Таргет: {target} TON\n"
                    f"Действие: Оффер ({'LIVE' if LIVE_MODE else 'DRY RUN'})\n"
                    f"Баланс: {balance}"
                )
                await _alert(msg)
                sniper_history.add_record({
                    "type": "offer", "gift_name": gift_name, "gift_id": gift_id,
                    "action_price_ton": target, "floor_price_ton": floor,
                    "balance_ton": balance, "mode": "live" if LIVE_MODE else "dry_run",
                    "result": None,
                })
                # Offers are NOT gated on _balance_allows() -- unlike a buy/bid,
                # an offer reserving TON below the floor gets fixed by
                # cancel_stale_offers() cancelling the oldest pending offer to
                # compensate (even before its own time-based cutoff), rather
                # than refusing to make new offers at all once balance is low.
                if LIVE_MODE and _spend_allowed(target):
                    try:
                        result = tonnel_market.create_offer(gift_id, target)
                        await _alert(f"Offer result for {gift_name}: {result}")
                        await _check_auth_error(result)
                        sniper_history.add_record({
                            "type": "offer_result", "gift_name": gift_name, "gift_id": gift_id,
                            "action_price_ton": target, "result": str(result),
                        })
                        # Note: an offer isn't counted against the session
                        # spend cap the way a buy/bid is (a seller may decline
                        # it) -- but it DOES reserve TON on Tonnel's side.
                        # cancel_stale_offers() finds this offer (and its real
                        # offer_id) later via tonnel_market.list_my_offers(),
                        # and will cancel the oldest pending offer if balance
                        # needs recovering -- create_offer()'s own response
                        # never includes an offer_id, which is why it's not
                        # tracked here directly.
                        succeeded = isinstance(result, dict) and result.get("status") == "success"
                        if not succeeded:
                            logger.warning(
                                "Offer for %s was not accepted by Tonnel: %r",
                                gift_name, result,
                            )
                    except Exception as e:
                        await _alert(f"Offer FAILED for {gift_name}: {e}")
                        sniper_history.add_record({
                            "type": "offer_result", "gift_name": gift_name, "gift_id": gift_id,
                            "action_price_ton": target, "result": f"FAILED: {e}",
                        })

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _bid_on_auction(entry: dict) -> None:
    """Actually places (or dry-run logs) a bid for an auction whose time
    has come -- factored out of watch_new_auctions() since it's called
    from the pending-auction sweep, not directly off the polled feed."""
    auction_id = entry["auction_id"]
    gift_name = entry["gift_name"]

    try:
        floor = await mrkt_market.get_floor_price(gift_name)
    except Exception as e:
        logger.warning("MRKT floor lookup failed for %r: %s", gift_name, e)
        return
    if floor is None:
        return
    if floor > MAX_GIFT_PRICE_TON:
        logger.info(
            "[AUCTION] %s (id %s): floor %.2f TON is above the %.2f TON cap, skipping.",
            gift_name, auction_id, floor, MAX_GIFT_PRICE_TON,
        )
        return

    target = _target_price(floor, AUCTION_DISCOUNT_PERCENT)
    balance = _balance_str()
    link = _nft_link(gift_name, entry.get("gift_num"), entry.get("gift_id") or auction_id)
    # NOTE: no fresh current-bid check here -- Tonnel's "new auctions" feed
    # only covers recently-created auctions, so there's no confirmed way to
    # re-fetch this specific auction's live current bid by the time its
    # bid window opens. place_bid() itself is authoritative: if someone's
    # already bid past target, it'll just fail and get logged below, no
    # money moves either way. "Цена" below is the last bid known at
    # discovery time, which may be stale by now.
    last_known_bid = entry.get("current_bid_ton")
    price_line = f"{last_known_bid} TON" if last_known_bid is not None else "нет ставок"
    msg = (
        f"Аукцион\n"
        f"{link}\n"
        f"Цена: {price_line}\n"
        f"Таргет: {target} TON\n"
        f"Действие: Ставка ({'LIVE' if LIVE_MODE else 'DRY RUN'})\n"
        f"Баланс: {balance}"
    )
    await _alert(msg)
    sniper_history.add_record({
        "type": "bid", "gift_name": gift_name, "auction_id": auction_id,
        "action_price_ton": target, "floor_price_ton": floor,
        "balance_ton": balance, "mode": "live" if LIVE_MODE else "dry_run",
        "result": None,
    })
    if LIVE_MODE and _spend_allowed(target) and _balance_allows(target):
        try:
            result = tonnel_market.place_bid(auction_id, target)
            _record_spend(target)  # counted conservatively; refunded if outbid later
            await _alert(f"Bid result for {gift_name}: {result}")
            await _check_auth_error(result)
            sniper_history.add_record({
                "type": "bid_result", "gift_name": gift_name, "auction_id": auction_id,
                "action_price_ton": target, "result": str(result),
            })
        except Exception as e:
            await _alert(f"Bid FAILED for {gift_name}: {e}")
            sniper_history.add_record({
                "type": "bid_result", "gift_name": gift_name, "auction_id": auction_id,
                "action_price_ton": target, "result": f"FAILED: {e}",
            })


async def watch_new_auctions(state: dict) -> None:
    """
    Discovers new auctions and registers them in state["pending_auctions"]
    without bidding yet -- the actual bid only fires once an auction has
    AUCTION_BID_WINDOW_HOURS or fewer left (see the sweep below), per
    request: no point committing to a bid hours before an auction ends.
    """
    seen = set(state["seen_auctions"])
    pending: list[dict] = state["pending_auctions"]

    if not seen:
        try:
            existing = tonnel_market.get_new_auctions()
            seen.update(item["auction_id"] for item in existing)
            state["seen_auctions"] = list(seen)
            _save_state(state)
            logger.info(
                "[AUCTION] No prior state -- marked %d currently-running auctions "
                "as already seen (won't act on the existing backlog, only on new "
                "auctions from here on).", len(existing),
            )
        except Exception as e:
            logger.warning("Priming seen-auctions failed: %s", e)

    while True:
        try:
            auctions = tonnel_market.get_new_auctions()
        except Exception as e:
            logger.warning("Tonnel new-auctions check failed: %s", e)
            await asyncio.sleep(_retry_delay(e, POLL_INTERVAL_SECONDS))
            continue

        for item in auctions:
            auction_id = item["auction_id"]
            if auction_id in seen:
                continue
            seen.add(auction_id)
            state["seen_auctions"] = list(seen)

            gift_name = item.get("gift_name") or ""
            if _is_blacklisted(gift_name):
                continue
            if _is_trade_banned(item):
                logger.info(
                    "[AUCTION] %s (id %s): still under Tonnel's transfer cooldown "
                    "-- skipping.", gift_name, auction_id,
                )
                continue
            ends_at_ts = item.get("ends_at_ts")
            if ends_at_ts is None:
                logger.warning(
                    "[AUCTION] %s (id %s): couldn't determine end time (no known "
                    "field name matched in Tonnel's response -- see "
                    "get_new_auctions() in tonnel_market.py) -- skipping, can't "
                    "safely apply the %.0fh bid window without it. Raw item: %r",
                    gift_name, auction_id, AUCTION_BID_WINDOW_HOURS, item.get("raw"),
                )
                continue

            pending.append({
                "auction_id": auction_id, "gift_id": item.get("gift_id"),
                "gift_name": gift_name, "gift_num": item.get("gift_num"),
                "current_bid_ton": item.get("current_bid_ton"),
                "ends_at_ts": ends_at_ts,
            })
            logger.info(
                "[AUCTION] %s (id %s): registered, ends in %.1fh -- will bid once "
                "%.0fh or fewer remain.", gift_name, auction_id,
                (ends_at_ts - time.time()) / 3600, AUCTION_BID_WINDOW_HOURS,
            )

        state["pending_auctions"] = pending
        _save_state(state)

        still_pending = []
        for entry in pending:
            seconds_remaining = entry["ends_at_ts"] - time.time()
            if seconds_remaining < 0:
                continue  # already ended, drop it
            if seconds_remaining > AUCTION_BID_WINDOW_HOURS * 3600:
                still_pending.append(entry)
                continue
            await _bid_on_auction(entry)
            # handled either way (bid attempted, or skipped for a real reason
            # like floor being too high) -- not re-added, won't retry forever

        pending = still_pending
        state["pending_auctions"] = pending
        _save_state(state)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def main() -> None:
    state = _load_state()
    mode = "LIVE (real orders will be placed)" if LIVE_MODE else "DRY RUN (alerts only)"
    logger.info(
        "Sniper starting -- mode: %s | offer discount: %.0f%%, auction discount: %.0f%% "
        "(min %.2f TON) | max gift price: %.1f TON | listing ceiling: floor +%.0f%% "
        "(auctions: no ceiling) | offer auto-cancel: %.0fh | auction bid window: %.0fh | "
        "poll every %ds | session spend cap: %.1f TON",
        mode, OFFER_DISCOUNT_PERCENT, AUCTION_DISCOUNT_PERCENT, MIN_DISCOUNT_TON,
        MAX_GIFT_PRICE_TON, MAX_PREMIUM_PERCENT,
        OFFER_AUTO_CANCEL_HOURS, AUCTION_BID_WINDOW_HOURS,
        POLL_INTERVAL_SECONDS, MAX_SESSION_SPEND_TON,
    )
    await asyncio.gather(
        watch_new_listings(state),
        watch_new_auctions(state),
        cancel_stale_offers(state),
    )


if __name__ == "__main__":
    asyncio.run(main())
