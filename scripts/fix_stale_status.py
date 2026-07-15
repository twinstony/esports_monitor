"""Fix stale 'live' matches whose start_time is in the future."""
import sys
import os
import io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from esports_monitor.config.manager import load_config
from esports_monitor.data.sqlite_storage import SQLiteStorage
from esports_monitor.utils.time_utils import from_utc_iso, now_utc

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
config = load_config(base_dir)
db_path = (config.get("database") or {}).get("path", "esports_history.db")
if base_dir and not os.path.isabs(db_path):
    db_path = os.path.join(base_dir, db_path)
storage = SQLiteStorage(db_path=db_path)

now = now_utc()
live_matches = storage.get_matches_by_status("live")
fixed = 0
for m in live_matches:
    start_str = m.get("start_time")
    if not start_str:
        continue
    start_dt = from_utc_iso(start_str)
    if start_dt and now < start_dt:
        # Start time is in the future, but status is "live" → fix to "discovered"
        storage.update_match_status(m["match_id"], "discovered")
        print(f"Fixed: {m['team_a']} vs {m['team_b']} (start={start_str} is future, was live → discovered)")
        fixed += 1

print(f"\nFixed {fixed} stale 'live' matches.")

# Show current status summary
for status in ("discovered", "live", "ended", "settled"):
    matches = storage.get_matches_by_status(status)
    print(f"{status}: {len(matches)} matches")
