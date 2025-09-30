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

# Import debug_states from app to check notifications debug flag
from app import debug_states

# Global flag to track if feeding should be stopped
stop_feeding_flag = False

# Global variables to be set during initialization
_app = None
_socketio = None

# Shared variable to track drain completion
drain_complete = {'status': False, 'reason': None}

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

def log_extended_feedback(message, plant_ip=None, status='debug', sio=None):
    """
    Log extended feedback only if the 'feeding-extended-log' debug option is enabled.
    Avoids circular import by accessing debug_states locally.
    """
    from app import debug_states
    if debug_states.get('feeding-extended-log', False):
        from .feeding_service import log_feeding_feedback
        log_feeding_feedback(message, plant_ip, status, sio)

def send_notification(alert_text: str):
    """
    Send notification to Discord and/or Telegram if enabled.
    """
    from app import send_notification as app_send_notification
    app_send_notification(alert_text)

def control_valve(plant_ip, valve_ip, valve_id, action, sio=None):
    """Control a valve (on/off) via the valve_relay API."""
    resolved_valve_ip = standardize_host_ip(valve_ip)
    if not resolved_valve_ip:
        log_feeding_feedback(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}", plant_ip, status='error', sio=sio)
        send_notification(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}")
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
            send_notification(f"Failed to turn {action} valve {valve_id} for plant {plant_ip}: {data.get('error')}")
            return False
    except Exception as e:
        log_feeding_feedback(f"Error controlling valve {valve_id} for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=sio)
        send_notification(f"Error controlling valve {valve_id} for plant {plant_ip}: {str(e)}")
        return False

def wait_for_valve_off(plant_ip, valve_ip, valve_id, valve_label, timeout=10, sio=None):
    """Wait for a valve to be turned off by the remote system."""
    resolved_valve_ip = standardize_host_ip(valve_ip)
    if not resolved_valve_ip:
        log_feeding_feedback(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}", plant_ip, status='error', sio=sio)
        send_notification(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}")
        return False
    start_time = time.time()
    while time.time() - start_time < timeout:
        if stop_feeding_flag:
            log_feeding_feedback(f"Feeding interrupted for plant {plant_ip}", plant_ip, status='error', sio=sio)
            send_notification(f"Feeding interrupted for plant {plant_ip}")
            return False
        with current_app.config['plant_lock']:
            plant_data = current_app.config['plant_data']
            valve_status = plant_data.get(plant_ip, {}).get('valve_info', {}).get('valve_relays', {}).get(valve_label, {}).get('status', 'unknown')
            log_extended_feedback(f"Checking valve {valve_id} ({valve_label}) status: {valve_status}", plant_ip, status='info', sio=sio)
            if valve_status == 'off':
                log_extended_feedback(f"Valve {valve_id} ({valve_label}) confirmed off for plant {plant_ip}", plant_ip, status='success', sio=sio)
                return True
        time.sleep(1)
    log_extended_feedback(f"Timeout waiting for valve {valve_id} ({valve_label}) to turn off for plant {plant_ip}", plant_ip, status='warning', sio=sio)
    send_notification(f"Timeout waiting for valve {valve_id} ({valve_label}) to turn off for plant {plant_ip}")
    return False

def wait_for_sensor(plant_ip, sensor_key, expected_triggered, timeout=600, retries=2, sio=None):
    """Wait for a water level sensor to reach the expected triggered state, requiring a state change."""
    with current_app.config['plant_lock']:
        plant_data = current_app.config['plant_data']
        sensor_label = plant_data.get(plant_ip, {}).get('water_level', {}).get(sensor_key, {}).get('label', sensor_key)
        initial_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get(sensor_key, {}).get('triggered', 'unknown')
    log_extended_feedback(f"Initial state for sensor {sensor_label} (triggered={initial_triggered}) for plant {plant_ip}", plant_ip, status='info', sio=sio)

    for attempt in range(retries):
        log_extended_feedback(f"Starting sensor wait for {sensor_label} (expected={expected_triggered}, attempt {attempt+1}/{retries}) for plant {plant_ip}", plant_ip, status='info', sio=sio)
        start_time = time.time()
        state_changed = False
        while time.time() - start_time < timeout:
            if stop_feeding_flag:
                log_feeding_feedback(f"Feeding interrupted for plant {plant_ip}", plant_ip, status='error', sio=sio)
                send_notification(f"Feeding interrupted for plant {plant_ip}")
                return False
            with current_app.config['plant_lock']:
                plant_data = current_app.config['plant_data']
                current_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get(sensor_key, {}).get('triggered', 'unknown')
                if plant_ip in plant_data and current_triggered == expected_triggered and current_triggered != initial_triggered:
                    state_changed = True
                    log_extended_feedback(f"Sensor {sensor_label} reached expected state (triggered={expected_triggered}) after change from {initial_triggered} for plant {plant_ip}", plant_ip, status='success', sio=sio)
                    return True
            time.sleep(1)
        if not state_changed:
            log_extended_feedback(f"Timeout waiting for sensor {sensor_label} to change to triggered={expected_triggered} for plant {plant_ip} (attempt {attempt+1}/{retries})", plant_ip, status='warning', sio=sio)
            if attempt == retries - 1:
                send_notification(f"Failed waiting for sensor {sensor_label} to change to triggered={expected_triggered} for plant {plant_ip} after {retries} attempts")
        if attempt < retries - 1:
            time.sleep(5)
    log_extended_feedback(f"Failed waiting for sensor {sensor_label} to change to triggered={expected_triggered} for plant {plant_ip} after {retries} attempts", plant_ip, status='error', sio=sio)
    return False

def monitor_drain_conditions(plant_ip, drain_valve_ip, drain_valve, drain_valve_label, settings, sio):
    """Monitor drain flow and empty sensor concurrently, setting drain_complete when either condition is met."""
    global drain_complete
    with _app.app_context():
        activation_flow_rate = settings.get('activation_flow_rate', 0.2)
        min_flow_rate = settings.get('min_flow_rate', 0.05)
        activation_delay = settings.get('activation_delay', 5)
        min_flow_check_delay = settings.get('min_flow_check_delay', 30)
        max_drain_time = settings.get('max_drain_time', 600)

        with current_app.config['plant_lock']:
            plant_data = current_app.config['plant_data']
            water_level = plant_data.get(plant_ip, {}).get('water_level', {})
            empty_sensor = next((k for k, v in water_level.items() if v.get('label') == 'Empty'), None)
            initial_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get(empty_sensor, {}).get('triggered', 'unknown') if empty_sensor else 'unknown'
            if not empty_sensor:
                log_extended_feedback(f"No Empty sensor configured for plant {plant_ip} in drain conditions monitor", plant_ip, status='error', sio=sio)
                send_notification(f"No Empty sensor configured for plant {plant_ip} in drain conditions monitor")
                drain_complete = {'status': False, 'reason': 'no_sensor'}
                return

        start_time = time.time()
        monitoring_started = False
        low_flow_start = None

        while time.time() - start_time < max_drain_time:
            if stop_feeding_flag:
                log_feeding_feedback(f"Feeding interrupted during drain conditions monitoring for plant {plant_ip}", plant_ip, status='error', sio=sio)
                send_notification(f"Feeding interrupted during drain conditions monitoring for plant {plant_ip}")
                drain_complete = {'status': False, 'reason': 'interrupted'}
                control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=sio)
                return

            with current_app.config['plant_lock']:
                current_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get(empty_sensor, {}).get('triggered', 'unknown')
            if current_triggered == False and current_triggered != initial_triggered:
                log_feeding_feedback(f"Empty sensor triggered during drain conditions monitoring for {plant_ip}, completing drain", plant_ip, status='success', sio=sio)
                control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=sio)
                drain_complete = {'status': True, 'reason': 'sensor_triggered'}
                return

            flow_rate = get_latest_drain_flow_rate()
            elapsed_time = time.time() - start_time
            log_extended_feedback(f"Drain flow for {plant_ip}: {flow_rate:.2f} Gal/min (elapsed: {elapsed_time:.1f}s)", plant_ip, status='info', sio=sio)

            if not monitoring_started and elapsed_time >= activation_delay:
                monitoring_started = True
                log_feeding_feedback(f"Starting flow monitoring for {plant_ip} after activation delay of {activation_delay}s", plant_ip, status='info', sio=sio)

            if monitoring_started:
                if flow_rate >= min_flow_rate:
                    low_flow_start = None
                else:
                    if low_flow_start is None:
                        low_flow_start = time.time()
                    if time.time() - low_flow_start >= min_flow_check_delay:
                        log_feeding_feedback(f"Drain flow dropped below {min_flow_rate} Gal/min for {min_flow_check_delay}s after monitoring started, considering bucket empty and proceeding to fill", plant_ip, status='warning', sio=sio)
                        send_notification(f"Drain flow dropped below {min_flow_rate} Gal/min for {min_flow_check_delay}s after monitoring started, considering bucket empty and proceeding to fill")
                        control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=sio)
                        drain_complete = {'status': True, 'reason': 'low_flow'}
                        return

            eventlet.sleep(1)

        log_feeding_feedback(f"Max drain time ({max_drain_time}s) exceeded for {plant_ip}, aborting drain and proceeding to fill as failsafe", plant_ip, status='warning', sio=sio)
        send_notification(f"Max drain time ({max_drain_time}s) exceeded for {plant_ip}, aborting drain")
        control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=sio)
        drain_complete = {'status': True, 'reason': 'timeout'}

def start_feeding_sequence(use_fresh=True, use_feed=True, sio=None):
    global stop_feeding_flag, drain_complete
    stop_feeding_flag = False
    drain_complete = {'status': False, 'reason': None}
    socketio_instance = sio or current_app.extensions.get('socketio')
    if not socketio_instance:
        print("[ERROR] SocketIO not available for feeding sequence")
        return "Failed to start feeding sequence: SocketIO not available"

    with current_app.app_context():
        current_app.config['feeding_sequence_active'] = True
        current_app.config['current_feeding_phase'] = 'init'
        current_app.config['current_plant_ip'] = None
        log_extended_feedback(f"Set feeding_sequence_active to True", status='debug')

    socketio_instance.emit('feeding_sequence_state', {'active': True}, namespace='/status')

    settings = load_settings()
    additional_plants = settings.get('additional_plants', [])
    log_feeding_feedback(f"Starting feeding sequence with use_fresh={use_fresh}, use_feed={use_feed}. Plants: {additional_plants}", status='info', sio=socketio_instance)

    remaining_plants = list(additional_plants)
    completed_plants = []
    message = []
    had_empty = False

    reset_fresh_total()
    reset_feed_total()
    reset_drain_total()
    log_feeding_feedback("Reset all total volumes at start of sequence", status='info', sio=socketio_instance)

    for plant_ip in additional_plants:
        if stop_feeding_flag:
            log_feeding_feedback(f"Stopping sequence early due to interruption. Completed: {', '.join(completed_plants) if completed_plants else 'None'}. Remaining: {', '.join(remaining_plants) if remaining_plants else 'None'}", status='error', sio=socketio_instance)
            send_notification(f"Stopping sequence early due to interruption. Completed: {', '.join(completed_plants) if completed_plants else 'None'}. Remaining: {', '.join(remaining_plants) if remaining_plants else 'None'}")
            break

        resolved_plant_ip = standardize_host_ip(plant_ip)
        if not resolved_plant_ip:
            log_feeding_feedback(f"Failed to resolve plant IP {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"Failed to resolve plant IP {plant_ip}")
            message.append(f"Failed {plant_ip}: Resolution error")
            remaining_plants.remove(plant_ip)
            continue

        if not validate_feeding_allowed(plant_ip):
            log_feeding_feedback(f"Feeding not allowed for plant {plant_ip}", plant_ip, status='warning', sio=socketio_instance)
            message.append(f"Skipped {plant_ip}: Not allowed")
            remaining_plants.remove(plant_ip)
            continue

        with current_app.app_context():
            current_app.config['current_feeding_phase'] = 'drain'
            current_app.config['current_plant_ip'] = plant_ip

        try:
            response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": True}, timeout=5)
            response.raise_for_status()
            log_extended_feedback(f"Set feeding_in_progress for plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)
        except Exception as e:
            log_extended_feedback(f"Failed to set feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"Failed to set feeding_in_progress for plant {plant_ip}: {str(e)}")
            message.append(f"Failed {plant_ip}: Set progress error")
            remaining_plants.remove(plant_ip)
            continue

        with current_app.config['plant_lock']:
            plant_data = current_app.config['plant_data']
            valve_info = plant_data.get(plant_ip, {}).get('valve_info', {})
            drain_valve_ip = valve_info.get('drain_valve_ip')
            drain_valve = valve_info.get('drain_valve')
            drain_valve_label = valve_info.get('drain_valve_label')
            fill_valve_ip = valve_info.get('fill_valve_ip')
            fill_valve = valve_info.get('fill_valve')
            fill_valve_label = valve_info.get('fill_valve_label')
            water_level = plant_data.get(plant_ip, {}).get('water_level', {})
            empty_sensor = settings.get('drain_sensor', 'sensor3')  # Assuming default
            full_sensor = settings.get('fill_sensor', 'sensor1')    # Assuming default

        if not drain_valve_ip or not drain_valve:
            log_feeding_feedback(f"No drain valve configured for plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"No drain valve configured for plant {plant_ip}")
            message.append(f"Failed {plant_ip}: No drain valve")
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue

        if not control_valve(plant_ip, drain_valve_ip, drain_valve, 'on', sio=socketio_instance):
            message.append(f"Failed {plant_ip}: Drain valve on error")
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue

        log_feeding_feedback(f"Starting drain for plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)

        drain_monitor_thread = eventlet.spawn(monitor_drain_conditions, plant_ip, drain_valve_ip, drain_valve, drain_valve_label, settings, socketio_instance)

        while not drain_complete['status']:
            if stop_feeding_flag:
                control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=socketio_instance)
                log_feeding_feedback(f"Interrupted drain for {plant_ip}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Interrupted drain for {plant_ip}")
                message.append(f"Stopped {plant_ip}: Interrupted during drain")
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to interruption", plant_ip, status='info', sio=socketio_instance)
                except Exception as e:
                    log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                    send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
                remaining_plants.remove(plant_ip)
                break
            time.sleep(1)

        if drain_complete['status']:
            log_feeding_feedback(f"Drain complete for plant {plant_ip}. Reason: {drain_complete['reason']}", plant_ip, status='info', sio=socketio_instance)
        else:
            log_feeding_feedback(f"Drain failed for plant {plant_ip}. Reason: {drain_complete['reason']}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"Drain failed for plant {plant_ip}. Reason: {drain_complete['reason']}")
            message.append(f"Failed {plant_ip}: Drain error")
            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off', sio=socketio_instance)
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue

        drain_complete = {'status': False, 'reason': None}  # Reset for next plant

        if not wait_for_valve_off(plant_ip, drain_valve_ip, drain_valve, drain_valve_label, sio=socketio_instance):
            log_feeding_feedback(f"Failed to confirm drain valve {drain_valve} ({drain_valve_label}) off for {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"Failed to confirm drain valve {drain_valve} ({drain_valve_label}) off for {plant_ip}")
            message.append(f"Failed {plant_ip}: Drain valve not off")
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue

        log_feeding_feedback(f"Drain complete for plant {plant_ip}. Drain valve confirmed off.", plant_ip, status='info', sio=socketio_instance)

        with current_app.app_context():
            current_app.config['current_feeding_phase'] = 'fill'
            current_app.config['current_plant_ip'] = plant_ip

        if not fill_valve_ip or not fill_valve:
            log_feeding_feedback(f"No fill valve configured for plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"No fill valve configured for plant {plant_ip}")
            message.append(f"Failed {plant_ip}: No fill valve")
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue

        if not control_valve(plant_ip, fill_valve_ip, fill_valve, 'on', sio=socketio_instance):
            message.append(f"Failed {plant_ip}: Fill valve on error")
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue

        log_feeding_feedback(f"Starting fill for plant {plant_ip}", plant_ip, status='info', sio=socketio_instance)

        if not full_sensor:
            log_feeding_feedback(f"No Full sensor configured for plant {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"No Full sensor configured for plant {plant_ip}")
            message.append(f"Failed {plant_ip}: No Full sensor")
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off', sio=socketio_instance)
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue
        log_feeding_feedback(f"Starting wait for Full sensor on {plant_ip}", plant_ip, status='info', sio=socketio_instance)
        if not wait_for_sensor(plant_ip, full_sensor, True, sio=socketio_instance):
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off', sio=socketio_instance)
            if stop_feeding_flag:
                log_feeding_feedback(f"Stopped {plant_ip}: Interrupted during filling", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Stopped {plant_ip}: Interrupted during filling. Completed: {', '.join(completed_plants) if completed_plants else 'None'}. Remaining: {', '.join(remaining_plants) if remaining_plants else 'None'}")
                message.append(f"Stopped {plant_ip}: Interrupted during filling")
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to interruption", plant_ip, status='info', sio=socketio_instance)
                except Exception as e:
                    log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                    send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
                stop_feeding_sequence()
            else:
                message.append(f"Failed {plant_ip}: Fill timeout or error")
                remaining_plants.remove(plant_ip)
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
                except Exception as e:
                    log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                    send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue

        # Emit fill_complete event when full sensor triggers
        socketio_instance.emit('fill_complete', {'plant_ip': plant_ip}, namespace='/status')
        log_extended_feedback(f"Emitted fill_complete event for {plant_ip}", plant_ip, status='debug', sio=socketio_instance)

        if not wait_for_valve_off(plant_ip, fill_valve_ip, fill_valve, fill_valve_label, sio=socketio_instance):
            log_feeding_feedback(f"Failed to confirm fill valve {fill_valve} ({fill_valve_label}) off for {plant_ip}", plant_ip, status='error', sio=socketio_instance)
            send_notification(f"Failed to confirm fill valve {fill_valve} ({fill_valve_label}) off for {plant_ip}")
            message.append(f"Failed {plant_ip}: Fill valve not turned off")
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off', sio=socketio_instance)
            remaining_plants.remove(plant_ip)
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_extended_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}")
            continue
        log_feeding_feedback(f"Fill complete for plant {plant_ip}. Fill valve confirmed off.", plant_ip, status='info', sio=socketio_instance)

        with current_app.app_context():
            current_app.config['current_feeding_phase'] = 'idle'
            current_app.config['current_plant_ip'] = None

        fresh_total = get_fresh_total_volume()
        feed_total = get_feed_total_volume()
        drain_total = get_drain_total_volume()
        log_feeding_feedback(f"Flow readings for plant {plant_ip}: Fresh: {fresh_total:.2f} Gal, Feed: {feed_total:.2f} Gal, Drain: {drain_total:.2f} Gal", plant_ip, status='info', sio=socketio_instance)

        log_feeding_feedback(f"Completed full feeding cycle for plant {plant_ip}. Moving to next plant.", plant_ip, status='info', sio=socketio_instance)
        completed_plants.append(plant_ip)
        remaining_plants.remove(plant_ip)

        # Check feed level after completing the current plant, before moving to the next one
        if use_feed and not had_empty:
            feed_level = get_feed_level()
            if feed_level == 'Empty':
                log_feeding_feedback(f"Feed reservoir ran out after completing plant {plant_ip}. Stopping feeding sequence.", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Feed reservoir ran out after completing plant {plant_ip}. Completed: {', '.join(completed_plants) if completed_plants else 'None'}. Remaining: {', '.join(remaining_plants) if remaining_plants else 'None'}")
                message.append(f"Stopped after {plant_ip}: Feed reservoir empty")
                stop_feeding_sequence()
                break

    with current_app.app_context():
        current_app.config['feeding_sequence_active'] = False
        current_app.config['current_feeding_phase'] = 'idle'
        current_app.config['current_plant_ip'] = None
        log_extended_feedback(f"Set feeding_sequence_active to False", status='debug')
    socketio_instance.emit('feeding_sequence_state', {'active': False}, namespace='/status')
    if not stop_feeding_flag:
        log_feeding_feedback(f"Completed full feeding cycle for all plants.", status='info', sio=socketio_instance)
        send_notification(f"Completed full feeding cycle for all plants: {'; '.join(message) if message else 'All plants processed successfully'}. Completed: {', '.join(completed_plants) if completed_plants else 'None'}. Remaining: {', '.join(remaining_plants) if remaining_plants else 'None'}")
    else:
        log_feeding_feedback(f"Feeding sequence terminated early.", status='info', sio=socketio_instance)

    if not message:
        message.append("No eligible plants processed")
    return "Feeding sequence completed: " + "; ".join(message)

def stop_feeding_sequence():
    """Stop the feeding sequence by emitting stop_feeding and turning off active valves."""
    global stop_feeding_flag
    if current_app.config.get('feeding_sequence_active', False):
        stop_feeding_flag = True
        with current_app.app_context():
            current_app.config['feeding_sequence_active'] = False
            current_app.config['current_feeding_phase'] = 'idle'
            current_app.config['current_plant_ip'] = None
            log_extended_feedback(f"Set feeding_sequence_active to False in stop_feeding_sequence", status='debug')
        plant_clients = current_app.config.get('plant_clients', {})
        plants_data = current_app.config.get('plant_data', {})
        message = []

        socketio_instance = current_app.config.get('socketio') or current_app.extensions.get('socketio')
        log_extended_feedback("Stopping feeding sequence for all plants", status='info', sio=socketio_instance)
        send_notification("Stopping feeding sequence for all plants")
        socketio_instance.emit('feeding_sequence_state', {'active': False}, namespace='/status')

        # Clean up local relays and pump
        from utils.settings_utils import load_settings
        from services.feed_pump_service import control_feed_pump
        settings = load_settings()
        feed_relay = settings.get('relay_ports', {}).get('feed_water')
        fresh_relay = settings.get('relay_ports', {}).get('fresh_water')

        def control_local_relay(relay_id, action, sio=None, plant_ip=None, status='info'):
            url = f"http://127.0.0.1:8000/api/valve_relay/{relay_id}/{action}"
            try:
                response = requests.post(url, timeout=5)
                response.raise_for_status()
                data = response.json()
                if data.get('status') == 'success':
                    log_feeding_feedback(f"Local relay {relay_id} turned {action}", plant_ip, status, sio)
                    return True
                else:
                    log_feeding_feedback(f"Failed to turn {action} local relay {relay_id}: {data.get('error')}", plant_ip, 'error', sio)
                    send_notification(f"Failed to turn {action} local relay {relay_id}: {data.get('error')}")
                    return False
            except Exception as e:
                log_feeding_feedback(f"Error controlling local relay {relay_id}: {str(e)}", plant_ip, 'error', sio)
                send_notification(f"Error controlling local relay {relay_id}: {str(e)}")
                return False

        if feed_relay:
            control_local_relay(feed_relay, 'off', socketio_instance)
        if fresh_relay:
            control_local_relay(fresh_relay, 'off', socketio_instance)
        control_feed_pump(state=0)
        log_feeding_feedback("Turned off local feed pump and relays", status='info', sio=socketio_instance)

        for plant_ip in plant_clients:
            if plant_ip not in plant_clients or not plant_clients[plant_ip].connected:
                continue

            resolved_plant_ip = standardize_host_ip(plant_ip)
            if not resolved_plant_ip:
                log_feeding_feedback(f"Failed to resolve plant IP {plant_ip} for stop", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to resolve plant IP {plant_ip} for stop")
                continue

            try:
                plant_clients[plant_ip].emit('stop_feeding', namespace='/status')
                log_extended_feedback(f"Emitted stop_feeding for plant {plant_ip}", plant_ip, status='success', sio=socketio_instance)
            except Exception as e:
                log_extended_feedback(f"Failed to emit stop_feeding for plant {plant_ip}: {str(e)}", plant_ip, status='error', sio=socketio_instance)
                send_notification(f"Failed to emit stop_feeding for plant {plant_ip}: {str(e)}")

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
    else:
        log_extended_feedback("Stop feeding sequence called but sequence already stopped", status='debug')
        return "Feeding sequence already stopped"

def initiate_local_feeding_support(plant_ip):
    pass  # Placeholder for future logic