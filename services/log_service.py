# File: services/log_service.py
import json
import os
from datetime import datetime
import threading
import time

# Define the log directory and file
LOG_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'logs')
SENSOR_LOG_FILE = os.path.join(LOG_DIR, 'sensor_log.jsonl')

def ensure_log_dir_exists():
    """
    Ensures the log directory exists.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

def log_event(data_dict, category='sensor'):
    log_file = os.path.join(LOG_DIR, f'{category}_log.jsonl')
    ensure_log_dir_exists()
    data_dict['timestamp'] = datetime.now().isoformat()
    with open(log_file, 'a') as f:
        f.write(json.dumps(data_dict) + '\n')

def log_reset_event(sensor, previous_total):
    """
    Logs a reset event for a flow sensor.
    """
    log_event({
        'event_type': 'reset',
        'sensor': sensor,
        'previous_total': previous_total
    }, category='flow')

def log_calibration_event(factors):
    """
    Logs a calibration update event.
    """
    log_event({
        'event_type': 'calibration_update',
        'factors': factors
    }, category='settings')

def log_sensor_reading(sensor_name, value, additional_data=None):
    data = {'event_type': 'sensor', 'sensor_name': sensor_name, 'value': value}
    if additional_data:
        data.update(additional_data)
    log_event(data, category=sensor_name)

def log_flow_periodically():
    while True:
        from api.fresh_flow import get_latest_flow_rate as get_fresh_flow_rate, get_total_volume as get_fresh_total_volume
        from api.feed_flow import get_latest_flow_rate as get_feed_flow_rate, get_total_volume as get_feed_total_volume
        from api.drain_flow import get_latest_flow_rate as get_drain_flow_rate, get_total_volume as get_drain_total_volume
        fresh_flow = get_fresh_flow_rate()
        fresh_total = get_fresh_total_volume()
        feed_flow = get_feed_flow_rate()
        feed_total = get_feed_total_volume()
        drain_flow = get_drain_flow_rate()
        drain_total = get_drain_total_volume()
        data = {
            'fresh_flow': fresh_flow,
            'fresh_total': fresh_total,
            'feed_flow': feed_flow,
            'feed_total': feed_total,
            'drain_flow': drain_flow,
            'drain_total': drain_total
        }
        log_event(data, category='flow')
        time.sleep(6 * 3600)  # 6 hours in seconds

# Start the periodic logging in a background thread
threading.Thread(target=log_flow_periodically, daemon=True).start()