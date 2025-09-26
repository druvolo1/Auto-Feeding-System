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

# Global flag to track if feeding should be stopped
stop_feeding_flag = False
feeding_sequence_active = False

def validate_feeding_allowed(plant_ip):
    with current_app.config['plant_lock']:
        plant_data = current_app.config['plant_data']
        if plant_ip in plant_data and plant_data[plant_ip].get('settings', {}).get('allow_remote_feeding', False):
            return True
        return False

def log_feeding_feedback(message, plant_ip=None, status='info'):
    """
    Log feeding feedback to both the UI (via SocketIO) and feeding.jsonl.
    """
    socketio = current_app.extensions['socketio']
    log_data = {
        'event_type': 'feeding_feedback',
        'message': message,
        'status': status,
        'timestamp': datetime.now().isoformat()
    }
    if plant_ip:
        log_data['plant_ip'] = plant_ip
    
    socketio.emit('feeding_feedback', log_data, namespace='/status')
    log_event(log_data, category='feeding')

def control_valve(plant_ip, valve_ip, valve_id, action):
    """Control a valve (on/off) via the valve_relay API."""
    resolved_valve_ip = standardize_host_ip(valve_ip)
    if not resolved_valve_ip:
        log_feeding_feedback(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}", plant_ip, status='error')
        return False
    url = f"http://{resolved_valve_ip}:8000/api/valve_relay/{valve_id}/{action}"
    try:
        response = requests.post(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'success':
            log_feeding_feedback(f"Valve {valve_id} turned {action} for plant {plant_ip}", plant_ip, status='success')
            return True
        else:
            log_feeding_feedback(f"Failed to turn {action} valve {valve_id} for plant {plant_ip}: {data.get('error')}", plant_ip, status='error')
            return False
    except Exception as e:
        log_feeding_feedback(f"Error controlling valve {valve_id} for plant {plant_ip}: {str(e)}", plant_ip, status='error')
        return False

def wait_for_valve_off(plant_ip, valve_ip, valve_id, valve_label, timeout=30):
    """Wait for a valve to be turned off by the remote system."""
    resolved_valve_ip = standardize_host_ip(valve_ip)
    if not resolved_valve_ip:
        log_feeding_feedback(f"Failed to resolve valve IP {valve_ip} for plant {plant_ip}", plant_ip, status='error')
        return False
    start_time = time.time()
    while time.time() - start_time < timeout:
        if stop_feeding_flag:
            log_feeding_feedback(f"Feeding interrupted by user for plant {plant_ip}", plant_ip, status='error')
            return False
        with current_app.config['plant_lock']:
            plant_data = current_app.config['plant_data']
            valve_status = plant_data.get(plant_ip, {}).get('valve_info', {}).get('valve_relays', {}).get(valve_label, {}).get('status', 'unknown')
            if valve_status == 'off':
                log_feeding_feedback(f"Valve {valve_id} ({valve_label}) confirmed off for plant {plant_ip}", plant_ip, status='success')
                return True
        time.sleep(1)  # Blocking sleep to wait for valve status update
    log_feeding_feedback(f"Timeout waiting for valve {valve_id} ({valve_label}) to turn off for plant {plant_ip}", plant_ip, status='error')
    return False

def wait_for_sensor(plant_ip, sensor_key, expected_triggered, timeout=600, retries=2):
    """Wait for a water level sensor to reach the expected triggered state."""
    with current_app.config['plant_lock']:
        plant_data = current_app.config['plant_data']
        sensor_label = plant_data.get(plant_ip, {}).get('water_level', {}).get(sensor_key, {}).get('label', sensor_key)
    for attempt in range(retries):
        log_feeding_feedback(f"Starting sensor wait for {sensor_label} (triggered={expected_triggered}, attempt {attempt+1}/{retries}) for plant {plant_ip}", plant_ip, status='info')
        start_time = time.time()
        counter = 0
        while time.time() - start_time < timeout:
            if stop_feeding_flag:
                log_feeding_feedback(f"Feeding interrupted by user for plant {plant_ip}", plant_ip, status='error')
                return False
            with current_app.config['plant_lock']:
                plant_data = current_app.config['plant_data']
                if plant_ip in plant_data and plant_data[plant_ip].get('water_level', {}).get(sensor_key, {}).get('triggered') == expected_triggered:
                    log_feeding_feedback(f"Sensor {sensor_label} reached expected state (triggered={expected_triggered}) for plant {plant_ip}", plant_ip, status='success')
                    return True
            time.sleep(1)  # Blocking sleep to ensure sequential processing
            counter += 1
            if counter % 5 == 0:
                log_feeding_feedback(f"Current status for sensor {sensor_label}: triggered={current_triggered}", plant_ip, status='info')
        log_feeding_feedback(f"Timeout waiting for sensor {sensor_label} to reach triggered={expected_triggered} for plant {plant_ip} (attempt {attempt+1}/{retries})", plant_ip, status='warning')
        if attempt < retries - 1:
            time.sleep(5)  # Wait before retrying
    log_feeding_feedback(f"Failed waiting for sensor {sensor_label} to reach triggered={expected_triggered} for plant {plant_ip} after {retries} attempts", plant_ip, status='error')
    return False

def start_feeding_sequence():
    """Start the feeding sequence for all eligible plants sequentially."""
    global stop_feeding_flag, feeding_sequence_active
    stop_feeding_flag = False
    feeding_sequence_active = True
    plant_clients = current_app.config.get('plant_clients', {})
    plants_data = current_app.config.get('plant_data', {})
    message = []

    log_feeding_feedback(f"Starting feeding sequence for {len(plant_clients)} plants", status='info')
    socketio = current_app.extensions['socketio']
    socketio.emit('feeding_sequence_state', {'active': True}, namespace='/status')

    if not plant_clients:
        log_feeding_feedback("No plants configured in plant_clients", status='error')
        feeding_sequence_active = False
        socketio.emit('feeding_sequence_state', {'active': False}, namespace='/status')
        return "No plants configured for feeding"

    for plant_ip in list(plant_clients.keys()):
        if stop_feeding_flag:
            log_feeding_feedback("Feeding sequence stopped by user", status='error')
            message.append("Feeding sequence stopped by user")
            break

        # Reset all 3 flow meters before starting drain for this plant
        reset_fresh_total()
        reset_feed_total()
        reset_drain_total()
        log_feeding_feedback(f"Reset all flow meters before processing plant {plant_ip}", plant_ip, status='info')

        log_feeding_feedback(f"Processing plant {plant_ip}", plant_ip, status='info')
        
        # Verify connection
        if plant_ip not in plant_clients or not plant_clients[plant_ip].connected:
            log_feeding_feedback(f"Failed to connect to plant {plant_ip}", plant_ip, status='error')
            message.append(f"Skipped {plant_ip}: Not connected")
            continue

        # Verify allow_remote_feeding
        if not validate_feeding_allowed(plant_ip):
            log_feeding_feedback(f"Remote feeding not allowed for plant {plant_ip}", plant_ip, status='info')
            message.append(f"Skipped {plant_ip}: Remote feeding not allowed")
            continue

        # Resolve plant IP for API call
        resolved_plant_ip = standardize_host_ip(plant_ip)
        if not resolved_plant_ip:
            log_feeding_feedback(f"Failed to resolve plant IP {plant_ip}", plant_ip, status='error')
            message.append(f"Skipped {plant_ip}: Failed to resolve IP")
            continue

        # Set feeding_in_progress
        try:
            # Emit start_feeding for remote state
            plant_clients[plant_ip].emit('start_feeding', namespace='/status')
            # Call API to set feeding_in_progress
            response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": True}, timeout=5)
            response.raise_for_status()
            log_feeding_feedback(f"Set feeding_in_progress for plant {plant_ip}", plant_ip, status='success')
        except Exception as e:
            log_feeding_feedback(f"Failed to set feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            message.append(f"Skipped {plant_ip}: Failed to start feeding")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e2:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e2)}", plant_ip, status='error')
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
            log_feeding_feedback(f"Missing valve information for plant {plant_ip}", plant_ip, status='error')
            message.append(f"Skipped {plant_ip}: Missing valve information")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue

        # Turn on drain valve
        log_feeding_feedback(f"Turning on drain valve {drain_valve} ({drain_valve_label}) at {drain_valve_ip} for plant {plant_ip}", plant_ip, status='info')
        if not control_valve(plant_ip, drain_valve_ip, drain_valve, 'on'):
            message.append(f"Failed {plant_ip}: Could not turn on drain valve")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue

        # Wait for drain completion (Empty sensor triggered)
        empty_sensor = next((k for k, v in water_level.items() if v.get('label') == 'Empty'), None)
        if not empty_sensor:
            log_feeding_feedback(f"No Empty sensor configured for plant {plant_ip}", plant_ip, status='error')
            message.append(f"Failed {plant_ip}: No Empty sensor")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue
        if not wait_for_sensor(plant_ip, empty_sensor, True):
            if stop_feeding_flag:
                control_valve(plant_ip, drain_valve_ip, drain_valve, 'off')
                log_feeding_feedback(f"Stopped {plant_ip}: User interrupted during draining", plant_ip, status='error')
                message.append(f"Stopped {plant_ip}: User interrupted during draining")
                # Reset feeding_in_progress on interruption
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to interruption", plant_ip, status='info')
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            else:
                message.append(f"Failed {plant_ip}: Drain timeout or error")
                # Reset feeding_in_progress on error
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue

        # Wait for drain valve to be turned off by remote system
        if not wait_for_valve_off(plant_ip, drain_valve_ip, drain_valve, drain_valve_label):
            log_feeding_feedback(f"Failed to confirm drain valve {drain_valve} ({drain_valve_label}) off for plant {plant_ip}", plant_ip, status='error')
            message.append(f"Failed {plant_ip}: Drain valve not turned off")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue
        log_feeding_feedback(f"Drain complete for plant {plant_ip}. Drain valve confirmed off.", plant_ip, status='info')

        # Turn on fill valve
        log_feeding_feedback(f"Turning on fill valve {fill_valve} ({fill_valve_label}) at {fill_valve_ip} for plant {plant_ip}", plant_ip, status='info')
        if not control_valve(plant_ip, fill_valve_ip, fill_valve, 'on'):
            message.append(f"Failed {plant_ip}: Could not turn on fill valve")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue

        # Wait for fill completion (Full sensor triggered)
        full_sensor = next((k for k, v in water_level.items() if v.get('label') == 'Full'), None)
        if not full_sensor:
            log_feeding_feedback(f"No Full sensor configured for plant {plant_ip}", plant_ip, status='error')
            message.append(f"Failed {plant_ip}: No Full sensor")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue
        if not wait_for_sensor(plant_ip, full_sensor, True):
            if stop_feeding_flag:
                control_valve(plant_ip, fill_valve_ip, fill_valve, 'off')
                log_feeding_feedback(f"Stopped {plant_ip}: User interrupted during filling", plant_ip, status='error')
                message.append(f"Stopped {plant_ip}: User interrupted during filling")
                # Reset feeding_in_progress on interruption
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to interruption", plant_ip, status='info')
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            else:
                message.append(f"Failed {plant_ip}: Fill timeout or error")
                # Reset feeding_in_progress on error
                try:
                    response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                    response.raise_for_status()
                    log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
                except Exception as e:
                    log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue

        # Wait for fill valve to be turned off by remote system
        if not wait_for_valve_off(plant_ip, fill_valve_ip, fill_valve, fill_valve_label):
            log_feeding_feedback(f"Failed to confirm fill valve {fill_valve} ({fill_valve_label}) off for plant {plant_ip}", plant_ip, status='error')
            message.append(f"Failed {plant_ip}: Fill valve not turned off")
            # Reset feeding_in_progress on error
            try:
                response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
                response.raise_for_status()
                log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip} due to error", plant_ip, status='info')
            except Exception as e:
                log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')
            continue
        log_feeding_feedback(f"Fill complete for plant {plant_ip}. Fill valve confirmed off.", plant_ip, status='info')

        log_feeding_feedback(f"Feeding completed for plant {plant_ip}", plant_ip, status='success')
        message.append(f"Completed {plant_ip}")

        # Log the current flow readings after feeding completion
        fresh_total = get_fresh_total_volume()
        feed_total = get_feed_total_volume()
        drain_total = get_drain_total_volume()
        log_feeding_feedback(f"Flow readings for plant {plant_ip}: Fresh: {fresh_total:.2f} Gal, Feed: {feed_total:.2f} Gal, Drain: {drain_total:.2f} Gal", plant_ip, status='info')

        # Ensure the entire feeding cycle is complete before moving to the next plant
        log_feeding_feedback(f"Completed full feeding cycle for plant {plant_ip}. Moving to next plant.", plant_ip, status='info')

    feeding_sequence_active = False
    socketio.emit('feeding_sequence_state', {'active': False}, namespace='/status')
    log_feeding_feedback(f"Completed full feeding cycle for all plants.", status='info')

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

    log_feeding_feedback("Stopping feeding sequence for all plants", status='info')
    socketio = current_app.extensions['socketio']
    socketio.emit('feeding_sequence_state', {'active': False}, namespace='/status')

    for plant_ip in plant_clients:
        if plant_ip not in plant_clients or not plant_clients[plant_ip].connected:
            continue

        resolved_plant_ip = standardize_host_ip(plant_ip)
        if not resolved_plant_ip:
            log_feeding_feedback(f"Failed to resolve plant IP {plant_ip} for stop", plant_ip, status='error')
            continue

        try:
            # Emit stop_feeding for remote state
            plant_clients[plant_ip].emit('stop_feeding', namespace='/status')
            # Call API to reset feeding_in_progress
            response = requests.post(f"http://{resolved_plant_ip}:8000/api/settings/feeding_status", json={"in_progress": False}, timeout=5)
            response.raise_for_status()
            log_feeding_feedback(f"Reset feeding_in_progress for plant {plant_ip}", plant_ip, status='success')
        except Exception as e:
            log_feeding_feedback(f"Failed to reset feeding_in_progress for plant {plant_ip}: {str(e)}", plant_ip, status='error')

        # Turn off active valves
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
            control_valve(plant_ip, drain_valve_ip, drain_valve, 'off')
            log_feeding_feedback(f"Turned off drain valve {drain_valve} ({drain_valve_label}) for plant {plant_ip}", plant_ip, status='success')

        if fill_valve_ip and fill_valve and valve_relays.get(fill_valve_label, {}).get('status') == 'on':
            control_valve(plant_ip, fill_valve_ip, fill_valve, 'off')
            log_feeding_feedback(f"Turned off fill valve {fill_valve} ({fill_valve_label}) for plant {plant_ip}", plant_ip, status='success')

        message.append(f"Stopped {plant_ip}")

    if not message:
        message.append("No plants were active")
    return "Feeding stopped: " + "; ".join(message)

def initiate_local_feeding_support(plant_ip):
    pass  # Placeholder for future logic