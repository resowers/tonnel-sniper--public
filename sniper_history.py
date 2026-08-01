"""
Persists a log of every action the sniper takes -- buys, offers, and
bids, in both dry-run and live mode -- to sniper_history.json next to
this script.

Kept separate from sniper_state.json (which only tracks "seen" ids, and
can be deleted/reset without losing anything of value) so the action
log survives independently.
"""

import json
import os
import time

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "sniper_history.json")
MAX_RECORDS = 1000


def add_record(record: dict) -> None:
    records = get_records()
    records.append({"timestamp": time.time(), **record})
    if len(records) > MAX_RECORDS:
        records = records[-MAX_RECORDS:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


def get_records() -> list[dict]:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []
