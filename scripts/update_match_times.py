"""Run discovery to update match start_times with correct eventStartTime."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Fix encoding for Windows
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from esports_monitor.config.manager import load_config
from esports_monitor.api.polymarket import PolymarketClient
from esports_monitor.api.clob import ClobClient
from esports_monitor.data.sqlite_storage import SQLiteStorage
from esports_monitor.discovery.service import (
    MarketDiscoveryService,
    persist_discovered_matches,
    mark_discovery_run,
)

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
config = load_config(base_dir)

poly_cfg = config.get("polymarket") or {}
gamma_client = PolymarketClient(
    api_base=poly_cfg.get("gamma_api_base"),
    timeout=poly_cfg.get("timeout", 10),
)

clob_client = None
try:
    clob_client = ClobClient(
        api_base=poly_cfg.get("clob_api_base"),
        timeout=poly_cfg.get("timeout", 10),
    )
except Exception as e:
    print(f"CLOB client init failed: {e}")

db_path = (config.get("database") or {}).get("path", "esports_history.db")
if base_dir and not os.path.isabs(db_path):
    db_path = os.path.join(base_dir, db_path)
storage = SQLiteStorage(db_path=db_path)

discovery_cfg = config.get("discovery") or {}
service = MarketDiscoveryService(
    gamma_client=gamma_client,
    clob_client=clob_client,
    config=discovery_cfg,
)

print("Running discovery to update match start_times...")
result = service.discover()
print(f"Result: {result.summary}")
print(f"New/updated matches: {len(result.new_matches)}")

# Persist
count = persist_discovered_matches(storage, result.new_matches)
print(f"Persisted {count} matches")

# Verify
print("\nUpdated matches in DB:")
matches = storage.get_all_matches()
for m in matches:
    print(f"  {m['status']:12} {m['game']:6} {m['team_a'][:15]:15} vs {m['team_b'][:15]:15} start={m['start_time']} end={m['end_time']}")

mark_discovery_run(storage, result.summary)
print("Done.")
