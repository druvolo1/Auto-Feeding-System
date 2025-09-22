# File: services/log_service.py
import json
import os
from datetime import datetime

# Define the log directory and file
LOG_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'logs')

def ensure_log_dir_exists():
    """
    Ensures the log directory exists.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

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