# File: services/log_service.py
import json
import os
import time
from datetime import datetime, timedelta

# Define the log directory and file
LOG_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'logs')

# Keep this much history in the local JSONL logs. Anything older is deleted by
# the daily prune; without it feeding_log.jsonl grows without bound (it reached
# 11 MB / 58k lines before this was added, which made the log page unusable).
LOG_RETENTION_DAYS = 14
PRUNE_INTERVAL_SECONDS = 24 * 3600

def ensure_log_dir_exists():
    """
    Ensures the log directory exists.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

def _prune_decision(line, cutoff_iso):
    """
    Decide the fate of one JSONL line. Returns the line to write (cleaned), or
    None to drop it.

    NUL bytes injected by an unclean shutdown are stripped first: they are not
    data, and leaving them in makes the whole file read as binary and hides the
    entry's real timestamp. A line that still will not parse is kept - we cannot
    prove it is old, and silently dropping unreadable data is worse than size.
    """
    cleaned = line.replace('\x00', '').strip()
    if not cleaned:
        return None
    try:
        entry = json.loads(cleaned)
    except Exception:
        return cleaned
    ts = entry.get('timestamp')
    if not isinstance(ts, str):
        return cleaned
    return cleaned if ts >= cutoff_iso else None

def prune_log_file(log_file, days=LOG_RETENTION_DAYS):
    """
    Rewrite a JSONL log keeping only entries from the last `days` days.
    Returns (kept, removed). Writes to a temp file and atomically replaces the
    original, so a crash mid-prune cannot leave a truncated log.
    """
    if not os.path.isfile(log_file):
        return (0, 0)

    cutoff_iso = (datetime.now() - timedelta(days=days)).isoformat()
    tmp_file = log_file + '.prune_tmp'
    kept = removed = 0
    changed = False

    try:
        with open(log_file, 'r', errors='replace') as src, open(tmp_file, 'w') as dst:
            for line in src:
                if not line.strip():
                    changed = True
                    continue
                keep = _prune_decision(line, cutoff_iso)
                if keep is None:
                    removed += 1
                    changed = True
                else:
                    dst.write(keep + '\n')
                    kept += 1
                    if keep + '\n' != line:
                        changed = True
        if changed:
            os.replace(tmp_file, log_file)
        else:
            os.remove(tmp_file)
    except Exception as e:
        print(f"[LOG] Failed to prune {log_file}: {e}")
        try:
            os.remove(tmp_file)
        except Exception:
            pass
        return (kept, 0)

    return (kept, removed)

def prune_all_logs(days=LOG_RETENTION_DAYS):
    """Prune every *_log.jsonl in the log directory."""
    ensure_log_dir_exists()
    results = {}
    for name in sorted(os.listdir(LOG_DIR)):
        if not name.endswith('_log.jsonl'):
            continue
        kept, removed = prune_log_file(os.path.join(LOG_DIR, name), days)
        results[name] = (kept, removed)
        if removed:
            print(f"[LOG] Pruned {name}: removed {removed} entries older than {days} days, kept {kept}")
    return results

def prune_logs_daily():
    """Prune on startup, then once a day."""
    while True:
        try:
            prune_all_logs()
        except Exception as e:
            print(f"[LOG] Daily prune failed: {e}")
        time.sleep(PRUNE_INTERVAL_SECONDS)

def log_event(data_dict, category='general'):
    log_file = os.path.join(LOG_DIR, f'{category}_log.jsonl')
    ensure_log_dir_exists()
    data_dict['timestamp'] = datetime.now().isoformat()
    with open(log_file, 'a') as f:
        f.write(json.dumps(data_dict) + '\n')

def log_reset_event(sensor, previous_total):
    """
    Logs a reset event for a flow sensor (flow meter logs).
    """
    log_event({
        'event_type': 'reset',
        'sensor': sensor,
        'previous_total': previous_total
    }, category='flow_meter')

def log_calibration_event(factors):
    """
    Logs a calibration update event.
    """
    log_event({
        'event_type': 'calibration_update',
        'factors': factors
    }, category='settings')

def log_feed_event(details):
    """
    Logs a feed event (feed log). Details can include amount, type, etc.
    """
    log_event({
        'event_type': 'feed',
        **details
    }, category='feed')