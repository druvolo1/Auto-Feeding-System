from flask import current_app
import eventlet
import requests
from .log_service import log_event
from datetime import datetime
from utils.mdns_utils import standardize_host_ip
import time
from services.fresh_flow_service import get_total_volume as get_fresh_total_volume, reset_total as reset_fresh_total
from services.feed_flow_service import get_total_volume as get_feed_total_volume, reset_total as reset_feed_total
from services.drain_flow_service import get_total_volume as get_drain_total_volume, reset_total as reset_drain_total
from utils.settings_utils import load_settings
from flask_socketio import SocketIO  # Import SocketIO explicitly
from app import app  # Import the Flask app instance from app.py

# Global flag to track if feeding should be stopped
stop_feeding_flag = False
feeding_sequence_active = False

# Global variables to be set during initialization
_app = None
_socketio = None

def initialize_feeding_service(app_instance, socketio_instance):
    """Initialize the feeding service with the Flask app and SocketIO instances."""
    global _app, _socketio
    _app = app_instance
    _socketio = socketio_instance

def validate_feeding_allowed(plant_ip):
    with current_app.config['plant_lock']:
        plant_data = current_app.config['plant_data']
        if plant_ip in plant_data and plant_data[plant_ip].get('settings', {}).get('allow_remote_feeding', False):
            return True
        return False

def log_feeding_feedback(message, plant_ip=None, status='info', sio=None):
    """
    Log feeding feedback to both the UI (via SocketIO) and feeding.jsonl.
    Use the provided socketio instance if available, otherwise fall back to global or current_app.
    """
    sio = sio or _socketio or current_app.extensions.get('socketio')
    if not sio:
        print(f"[WARNING] SocketIO not available for logging: {message}")
        return
    log_data = {
        'event_type': 'feeding_feedback',
        'message': message,
        'status': status,
        'timestamp': datetime.now().isoformat()
    }
    if plant_ip:
        log_data['plant_ip'] = plant_ip
    
    sio.emit('feeding_feedback', log_data, namespace='/status')
    log_event(log_data, category='feeding')

def control_valve(plant_ip, valve_ip, valve_id, action, sio=None):
    """Control a valve (on/off) via the valve_relay API."""
    resolved_valve_ip = standardize_host_ip(valve_ip)
    if not resolved_valve_ip:
        log_feeding_feedback(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}", plant_ip, status='error', sio=sio)
        return False
    url = f"http://{resolved_valve_ip}:8000/api/valve_relay/{valve_id}/{action}"
    try:
        response = requests.post(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'success':
            log_feeding_feedback(f"Valve {valve_id} turned {action} for plant {plant_ip}", plant_ip, status='success', sio=sio)
            return True
        else:
            log_feeding_feedback(f"Failed to turn {action} valve {valve_id} for plant {plant_ip}: {data.get('error')}", plant_ip, status='error', sio=sio)
            return False
    except Exception as e:
        log_feeding_feedback(f"Error controlling valve {valve_id} for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=sio)
        return False

def wait_for_valve_off(plant_ip, valve_ip, valve_id, valve_label, timeout=30, sio=None):
    """Wait for a valve to be turned off by the remote system."""
    resolved_valve_ip = standardize_host_ip(valve_ip)
    if not resolved_valve_ip:
        log_feeding_feedback(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}", plant_ip, status='error', sio=sio)
        return False
    start_time = time.time()
    while time.time() - start_time < timeout:
        if stop_feeding_flag:
            log_feeding_feedback(f"Feeding interrupted by user for plant {plant_ip}", plant_ip, status='error', sio=sio)
            return False
        with current_app.config['plant_lock']:
            plant_data = current_app.config['plant_data']
            valve_status = plant_data.get(plant_ip, {}).get('valve_info', {}).get('valve_relays', {}).get(valve_label, {}).get('status', 'unknown')
            if valve_status == 'off':
                log_feeding_feedback(f"Valve {valve_id} ({valve_label}) confirmed off for plant {plant_ip}", plant_ip, status='success', sio=sio)
                return True
        time.sleep(1)  # Blocking sleep to wait for valve status update
    log_feeding_feedback(f"Timeout waiting for valve {valve_id} ({valve_label}) to turn off for plant {plant_ip}", plant_ip, status='error', sio=sio)
    return False

def wait_for_sensor(plant_ip, sensor_key, expected_triggered, timeout=600, retries=2, sio=None):
    """Wait for a water level sensor to reach the expected triggered state, requiring a state change."""
    with current_app.config['plant_lock']:
        plant_data = current_app.config['plant_data']
        sensor_label = plant_data.get(plant_ip, {}).get('water_level', {}).get(sensor_key, {}).get('label', sensor_key)
        initial_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get(sensor_key, {}).get('triggered', 'unknown')
    log_feeding_feedback(f"Initial state for sensor {sensor_label} (triggered={initial_triggered}) for plant {plant_ip}", plant_ip, status='info', sio=sio)

    for attempt in range(retries):
        log_feeding_feedback(f"Starting sensor wait for {sensor_label} (expected={expected_triggered}, attempt {attempt+1}/{retries}) for plant {plant_ip}", plant_ip, status='info', sio=sio)
        start_time = time.time()
        counter = 0
        state_changed = False
        while time.time() - start_time < timeout:
            if stop_feeding_flag:
                log_feeding_feedback(f"Feeding interrupted by user for plant {plant_ip}", plant_ip, status='error', sio=sio)
                return False
            with current_app.config['plant_lock']:
                plant_data = current_app.config['plant_data']
                current_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get(sensor_key, {}).get('triggered', 'unknown')
                if plant_ip in plant_data and current_triggered == expected_triggered and current_triggered != initial_triggered:
                    state_changed = True
                    log_feeding_feedback(f"Sensor {sensor_label} reached expected state (triggered={expected_triggered}) after change from {initial_triggered} for plant {plant_ip}", plant_ip, status='success', sio=sio)
                    return True
            time.sleep(1)
            counter += 1
            if counter % 5 == 0:
                log_feeding_feedback(f"Current status for sensor {sensor_label}: triggered={current_triggered}", plant_ip, status='info', sio=sio)
        if not state_changed:
            log_feeding_feedback(f"Timeout waiting for sensor {sensor_label} to change to triggered={expected_triggered} for plant {plant_ip} (attempt {attempt+1}/{retries})", plant_ip, status='warning', sio=sio)
        if attempt < retries - 1:
            time.sleep(5)
    log_feeding_feedback(f"Failed waiting for sensor {sensor_label} to change to triggered={expected_triggered} for plant {plant_ip} after {retries} attempts", plant_ip, status='error', sio=sio)
    return False

def monitor_drain_flow(plant_ip, drain_valve_ip, drain_valve, drain_valve_label, settings, sio):
    """Monitor drain flow during the draining process and return flow status."""
    with _app.app_context():  # Use the globally set _app instance
        activation_flow_rate = settings.get('activation_flow_rate', 0.2)  # Default to 0.2 Gal/min
        min_flow_rate = settings.get('min_flow_rate', 0.05)  # Default to 0.05 Gal/min
        activation_delay = settings.get('activation_delay', 5)  # Default to 5 seconds
        min_flow_check_delay = settings.get('min_flow_check_delay', 30)  # Default to 30 seconds
        max_drain_time = settings.get('max_drain_time', 600)  # Default to 600 seconds

        start_time = time.time()
        flow_activated = False
        last_flow_time = start_time
        low_flow_detected = False

        while time.time() - start_time < max_drain_time:
            if stop_feeding_flag:
                log_feeding_feedback(f"Feeding interrupted by user during drain flow monitoring for plant {plant_ip}", plant_ip, status='error', sio=sio)
                return {'success': False, 'reason': 'interrupted'}

            current_flow = get_drain_total_volume()  # Get cumulative flow since last reset
            elapsed_time = time.time() - start_time

            if elapsed_time > 0:  # Avoid division by zero
                flow_rate = current_flow / (elapsed_time / 60)  # Convert to Gal/min
                log_feeding_feedback(f"Drain flow for {plant_ip}: {flow_rate:.2f} Gal/min (elapsed: {elapsed_time:.1f}s)", plant_ip, status='info', sio=sio)

                if not flow_activated:
                    if flow_rate >= activation_flow_rate:
                        if elapsed_time >= activation_delay:
                            flow_activated = True
                            log_feeding_feedback(f"Drain flow activated for {plant_ip} after {activation_delay}s", plant_ip, status='info', sio=sio)
                else:
                    if flow_rate < min_flow_rate:
                        if time.time() - last_flow_time >= min_flow_check_delay:
                            low_flow_detected = True
                            log_feeding_feedback(f"Low drain flow detected for {plant_ip} (< {min_flow_rate} Gal/min), aborting drain", plant_ip, status='warning', sio=sio)
                            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=sio)
                            return {'success': False, 'reason': 'low_flow'}
                        last_flow_time = time.time()

            eventlet.sleep(1)  # Non-blocking sleep

        if elapsed_time >= max_drain_time:
            log_feeding_feedback(f"Max drain time ({max_drain_time}s) exceeded for {plant_ip}, aborting drain", plant_ip, status='warning', sio=sio)
            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=sio)
            return {'success': False, 'reason': 'timeout'}

        log_feeding_feedback(f"Drain flow monitoring completed for {plant_ip} with normal flow", plant_ip, status='success', sio=sio)
        return {'success': True}

