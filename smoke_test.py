"""
One-off smoke test: checks each piece of the sniper's setup separately,
before running the full polling loop -- faster to spot a broken env var
or expired auth than waiting for a live match.

Usage:
    python smoke_test.py
"""

import asyncio

import mrkt_market
import tonnel_market


async def main() -> None:
    print("== 1. Tonnel: fetching newest listings ==")
    try:
        listings = tonnel_market.get_new_listings(limit=5)
        print(f"OK -- got {len(listings)} listings")
        for item in listings[:3]:
            print(" ", item)
    except Exception as e:
        print("FAILED:", e)
        return

    print("\n== 2. Tonnel: balance lookup ==")
    try:
        balance = tonnel_market.get_balance_ton()
        print("Balance:", balance, "TON" if balance is not None else "(field not found, see warning above)")
    except Exception as e:
        print("FAILED:", e)

    print("\n== 3. MRKT: floor price lookup (may prompt for phone/login code on first run) ==")
    test_gift_name = listings[0]["gift_name"] if listings else "Desk Calendar"
    try:
        floor = await mrkt_market.get_floor_price(test_gift_name)
        print(f"Floor for {test_gift_name!r}: {floor}")
    except Exception as e:
        print("FAILED:", e)

    print("\nDone. If all three sections say OK, sniper.py should run cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
