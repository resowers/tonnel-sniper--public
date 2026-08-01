# Tonnel/MRKT Sniper

A standalone script that watches [Tonnel](https://tonnel.network/) for
new gift listings and new auctions, prices each one against its
[MRKT](https://tgmrkt.io/) floor price, and automatically offers/bids/
buys at a discount.

**This executes real financial transactions once armed.** Read the
"Read this before turning it on" section below before setting
`SNIPER_LIVE_MODE=true`.

## What's in here

| File | Purpose |
|---|---|
| `sniper.py` | Main loop — watches Tonnel, prices against MRKT floor, auto offers/bids/buys |
| `tonnel_market.py` | Tonnel integration: listings, auctions, buy, bid, offer, cancel-offer |
| `mrkt_market.py` | MRKT integration: floor prices (the sniper's pricing reference) |
| `sniper_history.py` | Logs every sniper action (buy/offer/bid, dry-run or live) to `sniper_history.json` |
| `smoke_test.py` | One-off script to sanity-check your env vars/credentials before running the full sniper |
| `export_session_string.py` | One-off local helper to turn a logged-in MRKT session into a portable string, for deploying somewhere without an interactive terminal (e.g. Railway) |
| `test_session_bootstrap.py` | Simulates a fresh deploy locally, to verify `AMRKT_SESSION_STRING` actually works before pushing |
| `requirements.txt` | Pinned dependencies |

## How it works

Watches Tonnel for **new** fixed-price listings and **new** auctions
(anything that existed before the sniper started is snapshotted as
already-seen on startup, not acted on). For each new one, it looks up
the MRKT floor price for that gift's collection, and:

- **New listing, already priced at or below your target** → does an
  instant **buy** (guaranteed, no risk of the seller ignoring an offer).
- **New listing, priced above your target** → submits an **offer** at
  `floor_price × (1 − SNIPE_OFFER_DISCOUNT_PERCENT%)`.
- **New auction** → registers it, then places a **bid** at
  `floor_price × (1 − SNIPE_AUCTION_DISCOUNT_PERCENT%)` once
  `SNIPER_AUCTION_BID_WINDOW_HOURS` or fewer hours remain before it ends
  (not the moment it's discovered).

"Target" = `floor_price − max(floor_price × discount%, SNIPE_MIN_DISCOUNT_TON)`
— whichever cuts deeper, the percentage or the flat TON floor (so cheap
gifts still get a meaningful discount, not just a few cents off).
Offers and bids use separate percentages (defaults: 15% off for
offers, 10% off for auction bids); the flat 0.3 TON minimum is shared.

Other filters, all configurable via env vars:

- Any gift whose MRKT floor is above `SNIPER_MAX_GIFT_PRICE_TON`
  (default 10) is skipped entirely — since target is always ≤ floor,
  this caps every single action's spend at that amount too.
- Gifts still under Tonnel's transfer cooldown ("trade ban" — a
  `can_transfer_since` timestamp in the future) are skipped entirely —
  no point buying/bidding/offering on something that can't be resold
  or moved yet.
- Listings priced more than `SNIPE_MAX_PREMIUM_PERCENT` (default 15%)
  above MRKT floor are ignored outright, no offer submitted. This
  ceiling does **not** apply to auctions.
- `SNIPER_BLACKLIST` (comma-separated collection names, e.g.
  `"Santa Hat,Jelly Bunny"`) never gets acted on at all — matching
  ignores case and spaces.

**Offer cleanup**: a pending offer gets automatically cancelled once
either it's been pending longer than `SNIPE_OFFER_AUTO_CANCEL_HOURS`
(default 1h), or your live balance has dropped below
`SNIPER_MIN_BALANCE_TON` (default 50) — oldest pending offers get
cancelled first until it recovers. New offers are **not** blocked by
the balance floor the way buys/bids are (an offer goes through
regardless, and the oldest one gets cancelled to compensate if that
drops balance below the floor) — buys and bids, being
immediate/irreversible, are refused outright below the floor instead.

This cleanup works by polling `tonnel_market.list_my_offers()`
(Tonnel's own "my offers" list) rather than trying to remember an
offer's id from when it was submitted — `create_offer()`'s own response
never includes one. It runs on its own, much slower
`SNIPER_OFFER_CHECK_INTERVAL_SECONDS` (default 180s) rather than the
fast `POLL_INTERVAL_SECONDS` used for listing/auction discovery, and
pagination is capped to the last 24h of offer history — this endpoint
sits behind Cloudflare, and polling it aggressively with unbounded
pagination can trigger a rate-limit block that takes other Tonnel calls
down with it. If you see `Likely CloudFlare` 403s, all loops
automatically back off to `SNIPER_CLOUDFLARE_BACKOFF_SECONDS` (default
120s) instead of retrying fast, which would just prolong the block.

## Read this before turning it on

This script can spend real TON on your behalf. It ships with several
safety defaults, all overridable via env vars:

1. **Dry-run by default.** Nothing is actually bought/bid/offered
   until you set `SNIPER_LIVE_MODE=true`. Everything else still runs
   and logs/alerts exactly what it *would* have done — use this to
   watch it for a while before arming it.
2. **Session spend cap.** Even in live mode, `SNIPER_MAX_SESSION_SPEND_TON`
   (default 20) is checked before every buy/bid — it refuses to spend
   past that total in one run, as a backstop against bugs or
   unexpected repeat-fires.
3. **Live balance floor.** `SNIPER_MIN_BALANCE_TON` (default 50) is
   checked against your *live* Tonnel balance before every buy/bid, so
   the sniper can't run your balance down to where Tonnel starts
   rejecting everything outright.
4. **Persisted "seen" state** (`sniper_state.json`, gitignored) so
   restarting the script doesn't re-alert or re-buy the same listing
   twice.

### Setup
```bash
pip install -r requirements.txt

export TONNEL_AUTH_DATA="..."     # captured via DevTools, see below
export TG_API_ID="..."
export TG_API_HASH="..."          # MRKT (amrkt) auto-logs in with these

export SNIPE_OFFER_DISCOUNT_PERCENT="15"
export SNIPE_AUCTION_DISCOUNT_PERCENT="10"
export SNIPE_MIN_DISCOUNT_TON="0.3"
export SNIPER_MAX_GIFT_PRICE_TON="10"
export SNIPER_MIN_BALANCE_TON="50"
export SNIPER_LIVE_MODE="false"   # leave false until you've watched it run

python sniper.py
```

**Getting `TONNEL_AUTH_DATA`**: open the Tonnel mini-app in Telegram,
DevTools → Network → Fetch/XHR, find a request with an `authData` field
in its payload, copy that value. It's short-lived and needs periodic
re-capturing when the sniper starts logging auth errors.

**Getting `TG_API_ID`/`TG_API_HASH`**: free, from
[my.telegram.org](https://my.telegram.org). MRKT (via the `amrkt`
package) uses these to log in with your own Telegram account via
Pyrogram — on first run it'll prompt interactively for your phone
number and login code, then save a local session file
(`amrkt_session.session`, gitignored).

### Optional: Telegram alerts
Set both of these to get a Telegram message for every match (dry-run
or live), instead of just console output:
```bash
export TG_ALERT_BOT_TOKEN="your bot token"
export TG_ALERT_CHAT_ID="your telegram user id or a chat id"
```

### Action history
Every buy/offer/bid the sniper takes (dry-run or live) is appended to
`sniper_history.json` (gitignored) via `sniper_history.py` —
timestamp, gift, action price, floor price, your balance at the time,
mode, and (once submitted) the raw result.

### Deploying somewhere without an interactive terminal (e.g. Railway)

MRKT's login is interactive by default (phone number + code), which
doesn't work on a host with no TTY. Run `export_session_string.py`
once locally (after you've already logged in and
`amrkt_session.session` exists) to get a portable session string, set
it as `AMRKT_SESSION_STRING` on the host, and `mrkt_market.py`
materializes the actual session file from it automatically on first
startup there — no prompt needed. Verify it works with
`test_session_bootstrap.py` locally before deploying.

## Honest limitations, please read

- **The Tonnel "offer" feature (`create_offer`/`cancel_offer`/
  `list_my_offers`) is not part of the `tonnelmp` library** — all three
  were hand-captured from the Tonnel web app's own network requests and
  are DevTools-confirmed, on two different hosts
  (`gifts.coffin.meme` for create/cancel, `gifts2.tonnel.network` for
  listing) — see the module docstring in `tonnel_market.py`.
  `create_offer`'s own response never includes an offer id
  (`{"status": "success", "message": "success"}` and nothing else), so
  auto-cancel works by polling `list_my_offers()` instead of trying to
  remember one from submission time.
- **`get_new_auctions()`'s exact field names were mostly guesses,
  now partially confirmed from a live response**: `auctionEndTime`
  (nested under `item["auction"]`, as an ISO string) and `gift_num` are
  confirmed. The "current bid once someone has actually bid" field
  isn't — only the no-bids-yet case (`auction.startingBid`) has been
  seen live; see `_parse_current_bid()` in `tonnel_market.py`.
- **MRKT (`mrkt_market.py`) field names are confirmed** against amrkt's
  own documented data models (`floor_price_ton`, `sale_price_ton`,
  etc.).
- **`get_balance_ton()`'s field name is a best-effort guess** — tonnelmp's
  `info()` docs only say it returns "a dictionary containing balances,
  memo etc.", not the exact key. It checks a few plausible names and
  logs the raw response if none match; if your alerts show "Balance:
  unknown", check the logged dict and add the right key in
  `tonnel_market.py`.
- **Tonnel's own direct-buy and bidding (`buyGift`, `placeBid`) are
  documented by `tonnelmp` itself** and are the most likely to work
  correctly of the "action" functions here.
- **These are unofficial, reverse-engineered integrations**, not
  sanctioned by Tonnel or MRKT. They can break if those platforms
  change their APIs, and using them may not be within those platforms'
  own terms of service.
- **Auth expires.** `TONNEL_AUTH_DATA` is short-lived and needs manual
  DevTools re-capture periodically. The sniper detects an explicit
  "invalid/expired auth" response from Tonnel and alerts once loudly
  when it happens, but 403s from `tonnelmp` itself are indistinguishable
  from a Cloudflare rate-limit (it labels every 403 that way) — check
  for an explicit auth-error alert first if things stop working.
- **Floor prices can be manipulated (wash trading, thin collections).**
  A floor price from a single low-volume listing isn't necessarily a
  reliable reference — sanity-check collections before trusting the
  sniper on them.
- **Persisted state is local**, not committed to git
  (`sniper_state.json`, `sniper_history.json`, `*.session` are all
  gitignored). On an ephemeral host (e.g. Railway without a volume),
  this resets on every redeploy — the sniper re-snapshots the current
  backlog as already-seen on startup, so this is safe, just means it
  "forgets" history across deploys.

## Deploying to Railway

1. Push this repo to GitHub, then Railway → New Project → Deploy from
   GitHub repo.
2. Add the env vars from the Setup section above.
3. Set a custom start command: `python sniper.py`.
4. Leave `SNIPER_LIVE_MODE=false` until you've watched the logs for a
   while and are confident in the pricing/filters.
