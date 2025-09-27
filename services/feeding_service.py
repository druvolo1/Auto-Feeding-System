from flask import current_app
import eventlet
import requests
from .log_service import log_event
from datetime import datetime
from utils.mdns_utils import standardize_host_ip
import time
from services.fresh_flow_service import get_latest_flow_rate as get_latest_fresh_flow_rate, get_total_volume as get_fresh_total_volume, reset_total as reset_fresh_total, flow_reader as fresh_flow_reader
from services.feed_flow_service import get_latest_flow_rate as get_latest_feed_flow_rate, get_total_volume as get_feed_total_volume, reset_total as reset_feed_total, flow_reader as feed_flow_reader
from services.drain_flow_service import get_latest_flow_rate as get_latest_drain_flow_rate, get_total_volume as get_drain_total_volume, reset_total as reset_drain_total, flow_reader as drain_flow_reader
from services.valve_relay_service import reinitialize_relay_service, get_relay_status
from services.feed_level_service import get_feed_level
from utils.settings_utils import load_settings
from flask_socketio import SocketIO

# Status namespace
from status_namespace import StatusNamespace, set_socketio_instance

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

def wait_for_valve_off(plant_ip, valve_ip, valve_id, valve_label, timeout=10, sio=None):  # Reduced timeout to 10s
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
            log_feeding_feedback(f"Checking valve {valve_id} ({valve_label}) status: {valve_status}", plant_ip, status='info', sio=sio)
            if valve_status == 'off':
                log_feeding_feedback(f"Valve {valve_id} ({valve_label}) confirmed off for plant {plant_ip}", plant_ip, status='success', sio=sio)
                return True
        time.sleep(1)  # Blocking sleep to wait for valve status update
    log_feeding_feedback(f"Timeout waiting for valve {valve_id} ({valve_label}) to turn off for plant {plant_ip}", plant_ip, status='warning', sio=sio)
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

        # Get the empty sensor key
        with current_app.config['plant_lock']:
            plant_data = current_app.config['plant_data']
            water_level = plant_data.get(plant_ip, {}).get('water_level', {})
            empty_sensor = next((k for k, v in water_level.items() if v.get('label') == 'Empty'), None)
            if not empty_sensor:
                log_feeding_feedback(f"No Empty sensor configured for plant {plant_ip} in drain flow monitor", plant_ip, status='error', sio=sio)
                return {'success': False, 'reason': 'no_sensor'}

        start_time = time.time()
        flow_activated = False
        last_flow_time = start_time
        low_flow_detected = False

        while time.time() - start_time < max_drain_time:
            if stop_feeding_flag:
                log_feeding_feedback(f"Feeding interrupted by user during drain flow monitoring for plant {plant_ip}", plant_ip, status='error', sio=sio)
                return {'success': False, 'reason': 'interrupted'}

            # Check if empty sensor is triggered
            with current_app.config['plant_lock']:
                current_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get(empty_sensor, {}).get('triggered', 'unknown')
            if current_triggered == True:
                log_feeding_feedback(f"Empty sensor triggered during drain flow monitoring for {plant_ip}, completing drain", plant_ip, status='success', sio=sio)
                control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=sio)  # Ensure off
                return {'success': True, 'reason': 'sensor_triggered'}

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

def start_feeding_sequence():
    """Start the feeding sequence for all eligible plants sequentially."""
    global stop_feeding_flag, feeding_sequence_active
    stop_feeding_flag = False
    feeding_sequence_active = True
    plant_clients = current_app.config.get('plant_clients', {})
    plants_data = current_app.config.get('plant_data', {})
    settings = load_settings().get('drain_flow_settings', {})
    message = []

    log_feeding_feedback(f"Starting feeding sequence for {len(plant_clients)} plants")
    socketio_instance = current_app.extensions.get('socketio')  # Get socketio instance
    if not socketio_instance:
        raise RuntimeError("SocketIO extension not found in current_app.extensions")
    socketio_instance.emit('feeding_sequence_state', {'active': True}, namespace='/status')

    if not plant_clients:
        log_feeding_feedback("No plants configured in plant_clients", status='error', sio=socketio_instance)
        feeding_sequence_active = False
        socketio_instance.emit('feeding_sequence_state', {'active': False}, namespace='/status')
        return "No plants configured for feeding"

    for plant_ip in list(plant_clients.keys()):
        if stop_feeding_flag:
            log_feeding_feedback("Feeding sequence stopped by user", status='error', sio=socketio_instance)
            message.append("Feeding sequence stopped by user")
            break

        # Reset all 3 flow meters before starting drain for this plant
        reset_fresh_total()
        reset_feed_total()
        reset_drain_total()
        log_feeding_feedback(f"Reset all flow meters before processing plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)

        log_feeding_feedback(f"Processing plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)
        
        # Verify connection
        if plant_ip not in plant_clients or not plant_clients[plant_ip].connected:
            log_feeding_feedback(f"Failed to connect to plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            message.append(f"Skipped {plant_ip}: Not connected")
            continue

        # Verify allow_remote_feeding
        if not validate_feeding_allowed(plant_ip):
            log_feeding_feedback(f"Remote feeding not allowed for plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)
            message.append(f"Skipped {plant_ip}: Remote feeding not allowed")
            continue

        # Resolve plant IP for API call
        resolved_plant_ip = standardize_host_ip(plant_ip)
        if not resolved_plant_ip:
            log_feeding_feedback(f"Failed to resolve plant IP {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            message.append(f"Skipped {plant_ip}: Failed to resolve IP")
            continue

        # Set feeding_in_progress
        try:
            plant_clients[plant_ip].emit('start_feeding', namespace='/status')
            response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": True}, timeout=5)
            response.raise_for_status()
            log_feeding_feedback(f"Set feeding_in_progress for plant {plant_ip}", plant_ip, status='success', sio=socketio_instance)
        except Exception as e:
            log_feeding_feedback(f"Failed to set feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            message.append(f"Skipped {plant_ip}: Failed to start feeding")
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e2:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e2)}", plant_ip, status='error', sio=socketio_instance)
            continue

        # Get valve and sensor information from plant_data
        with current_app.config['plant_lock']:
            plant_data = plants_data.get(plant_ip, {})
            valve_info = plant_data.get('valve_info', {})
            water_level = plant_data.get('water_level', {})
            drain_valve_ip = valve_info.get('drain_valve_ip')
            drain_valve = valve_info.get('drain_valve')
            drain_valve_label = valve_info.get('drain_valve_label')
            fill_valve_ip = valve_info.get('fill_valve_ip')
            fill_valve = valve_info.get('fill_valve')
            fill_valve_label = valve_info.get('fill_valve_label')

        if not all([drain_valve_ip, drain_valve, fill_valve_ip, fill_valve]):
            log_feeding_feedback(f"Missing valve information for plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            message.append(f"Skipped {plant_ip}: Missing valve information")
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue

        # Turn on drain valve
        log_feeding_feedback(f"Turning on drain valve {drain_valve} ({drain_valve_label}) at {drain_valve_ip} for plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)
        if not control_valve(plant_ip, drain_valve_ip, drain_valve, 'on', sio=socketio_instance):
            message.append(f"Failed {plant_ip}: Could not turn on drain valve")
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue

        # Monitor drain flow concurrently with sensor wait
        flow_monitor = eventlet.spawn(monitor_drain_flow, plant_ip, drain_valve_ip, drain_valve, drain_valve_label, settings, socketio_instance)

        # Wait for drain completion (Empty sensor triggered)
        empty_sensor = next((k for k, v in water_level.items() if v.get('label') == 'Empty'), None)
        if not empty_sensor:
            log_feeding_feedback(f"No Empty sensor configured for plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            message.append(f"Failed {plant_ip}: No Empty sensor")
            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=socketio_instance)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue
        log_feeding_feedback(f"Starting wait for Empty sensor on {plant_ip}", plant_ip, status='info', sio=socketio_instance)
        sensor_result = wait_for_sensor(plant_ip, empty_sensor, True, sio=socketio_instance)

        # Get flow monitor result
        flow_result = flow_monitor.wait()
        if not flow_result['success']:
            log_feeding_feedback(f"Drain flow issue for {plant_ip}: {flow_result['reason']}, proceeding to fill", plant_ip, status='warning', sio=socketio_instance)
            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=socketio_instance)  # Stop drain if flow issue
        elif flow_result.get('reason') == 'sensor_triggered':
            log_feeding_feedback(f"Drain completed for {plant_ip} due to empty sensor trigger in flow monitor", plant_ip, status='success', sio=socketio_instance)

        # Prioritize sensor or low flow: Proceed to fill if sensor triggered or low flow detected
        if sensor_result or (not flow_result['success'] and flow_result['reason'] == 'low_flow'):
            if not sensor_result and flow_result['reason'] == 'low_flow':
                log_feeding_feedback(f"Low drain flow detected, but empty sensor not triggered. Possible root obstruction on sensor. Considering bucket empty and proceeding to fill.", plant_ip, status='warning', sio=socketio_instance)
            else:
                log_feeding_feedback(f"Empty sensor triggered for {plant_ip}, proceeding to fill", plant_ip, status='info', sio=socketio_instance)
        else:
            if stop_feeding_flag:
                control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=socketio_instance)
                log_feeding_feedback(f"Stopped {plant_ip}: User interrupted during draining", plant_ip, status='error', sio=socketio_instance)
                message.append(f"Stopped {plant_ip}: User interrupted during draining")
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to interruption", plant_ip, status='info', sio=socketio_instance)
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            else:
                message.append(f"Failed {plant_ip}: Drain timeout or error")
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue

        # Attempt to confirm drain valve off (non-blocking, proceed on timeout)
        valve_off_confirmed = wait_for_valve_off(plant_ip, drain_valve_ip, drain_valve, drain_valve_label, timeout=10, sio=socketio_instance)
        if not valve_off_confirmed:
            log_feeding_feedback(f"Could not confirm drain valve {drain_valve} ({drain_valve_label}) off for {plant_ip}, proceeding to fill", plant_ip, status='warning', sio=socketio_instance)
            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=socketio_instance)  # Ensure off
        else:
            log_feeding_feedback(f"Drain complete for plant {plant_ip}. Drain valve confirmed off.", plant_ip, status='info', sio=socketio_instance)

        # Turn on fill valve (proceed if sensor triggered)
        log_feeding_feedback(f"Turning on fill valve {fill_valve} ({fill_valve_label}) at {fill_valve_ip} for plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)
        if not control_valve(plant_ip, fill_valve_ip, fill_valve, 'on', sio=socketio_instance):
            message.append(f"Failed {plant_ip}: Could not turn on fill valve")
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue

        # Wait for fill completion (Full sensor triggered)
        full_sensor = next((k for k, v in water_level.items() if v.get('label') == 'Full'), None)
        if not full_sensor:
            log_feeding_feedback(f"No Full sensor configured for plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            message.append(f"Failed {plant_ip}: No Full sensor")
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off', sio=socketio_instance)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue
        log_feeding_feedback(f"Starting wait for Full sensor on {plant_ip}", plant_ip, status='info', sio=socketio_instance)
        if not wait_for_sensor(plant_ip, full_sensor, False, sio=socketio_instance):
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off', sio=socketio_instance)
            if stop_feeding_flag:
                log_feeding_feedback(f"Stopped {plant_ip}: User interrupted during filling", plant_ip, status='error', sio=socketio_instance)
                message.append(f"Stopped {plant_ip}: User interrupted during filling")
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to interruption", plant_ip, status='info', sio=socketio_instance)
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            else:
                message.append(f"Failed {plant_ip}: Fill timeout or error")
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue

        # Wait for fill valve to be turned off by remote system
        if not wait_for_valve_off(plant_ip, fill_valve_ip, fill_valve, fill_valve_label, sio=socketio_instance):
            log_feeding_feedback(f"Failed to confirm fill valve {fill_valve} ({fill_valve_label}) off for plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            message.append(f"Failed {plant_ip}: Fill valve not turned off")
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off', sio=socketio_instance)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            continue
        log_feeding_feedback(f"Fill complete for plant {plant_ip}. Fill valve confirmed off.", plant_ip, status='info', sio=socketio_instance)

        # Log the current flow readings after feeding completion
        fresh_total = get_fresh_total_volume()
        feed_total = get_feed_total_volume()
        drain_total = get_drain_total_volume()
        log_feeding_feedback(f"Flow readings for plant {plant_ip}: Fresh: {fresh_total:.2f} Gal, Feed: {feed_total:.2f} Gal, Drain: {drain_total:.2f} Gal", plant_ip, status='info', sio=socketio_instance)

        # Ensure the entire feeding cycle is complete before moving to the next plant
        log_feeding_feedback(f"Completed full feeding cycle for plant {plant_ip}. Moving to next plant.", plant_ip, status='info', sio=socketio_instance)

    feeding_sequence_active = False
    socketio_instance.emit('feeding_sequence_state', {'active': False}, namespace='/status')
    log_feeding_feedback(f"Completed full feeding cycle for all plants.", status='info', sio=socketio_instance)

    if not message:
        message.append("No eligible plants processed")
    return "Feeding sequence completed: " + "; ".join(message)

def stop_feeding_sequence():
    """Stop the feeding sequence by emitting stop_feeding and turning off active valves."""
    global stop_feeding_flag, feeding_sequence_active
    stop_feeding_flag = True
    feeding_sequence_active = False
    plant_clients = current_app.config.get('plant_clients', {})
    plants_data = current_app.config.get('plant_data', {})
    message = []

    socketio_instance = current_app.extensions.get('socketio')  # Get socketio instance
    log_feeding_feedback("Stopping feeding sequence for all plants", status='info', sio=socketio_instance)
    socketio_instance.emit('feeding_sequence_state', {'active': False}, namespace='/status')

    for plant_ip in plant_clients:
        if plant_ip not in plant_clients or not plant_clients[plant_ip].connected:
            continue

        resolved_plant_ip = standardize_host_ip(plant_ip)
        if not resolved_plant_ip:
            log_feeding_feedback(f"Failed to resolve plant IP {plant_ip} for stop", plant_ip, status='error', sio=socketio_instance)
            continue

        try:
            plant_clients[plant_ip].emit('stop_feeding', namespace='/status')
            response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
            response.raise_for_status()
            log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip}", plant_ip, status='success', sio=socketio_instance)
        except Exception as e:
            log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)

        with current_app.config['plant_lock']:
            plant_data = plants_data.get(plant_ip, {})
            valve_info = plant_data.get('valve_info', {})
            drain_valve_ip = valve_info.get('drain_valve_ip')
            drain_valve = valve_info.get('drain_valve')
            drain_valve_label = valve_info.get('drain_valve_label')
            fill_valve_ip = valve_info.get('fill_valve_ip')
            fill_valve = valve_info.get('fill_valve')
            fill_valve_label = valve_info.get('fill_valve_label')
            valve_relays = valve_info.get('valve_relays', {})

        if drain_valve_ip and drain_valve and valve_relays.get(drain_valve_label, {}).get('status') == 'on':
            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=socketio_instance)
            log_feeding_feedback(f"Turned off drain valve {drain_valve} ({drain_valve_label}) for plant {plant_ip}", plant_ip, status='success', sio=socketio_instance)

        if fill_valve_ip and fill_valve and valve_relays.get(fill_valve_label, {}).get('status') == 'on':
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off', sio=socketio_instance)
            log_feeding_feedback(f"Turned off fill valve {fill_valve} ({fill_valve_label}) for plant {plant_ip}", plant_ip, status='success', sio=socketio_instance)

        message.append(f"Stopped {plant_ip}")

    if not message:
        message.append("No plants were active")
    return "Feeding stopped: " + "; ".join(message)

def initiate_local_feeding_support(plant_ip):
    pass  # Placeholder for future logic