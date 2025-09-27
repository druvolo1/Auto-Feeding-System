import eventlet
import time
from flask import Flask, current_app
from datetime import datetime
from services.valve_relay_service import turn_on_relay, turn_off_relay
from services.feed_pump_service import control_feed_pump as pump_control  # Renamed to avoid recursion
from utils.settings_utils import load_settings
from .log_service import log_event
from services.feed_flow_service import get_total_volume as get_feed_total_volume, get_latest_flow_rate as get_latest_feed_flow_rate
from services.fresh_flow_service import get_total_volume as get_fresh_total_volume, get_latest_flow_rate as get_latest_fresh_flow_rate
import sys
import os

# Global flag for mixing
stop_mixing_flag = False

def log_mixing_feedback(message, status='info', sio=None, app=None):
    """
    Log mixing feedback to both the UI (via SocketIO) and feeding.jsonl.
    """
    if not sio or not app:
        print(f"[WARNING] SocketIO or app not provided for logging: {message}")
        return
    with app.app_context():
        sio_instance = sio or app.extensions.get('socketio')
        if not sio_instance:
            print(f"[WARNING] SocketIO not available for logging: {message}")
            return
        log_data = {
            'event_type': 'mixing_feedback',
            'message': message,
            'status': status,
            'timestamp': datetime.now().isoformat()
        }
        
        sio_instance.emit('feeding_feedback', log_data, namespace='/status')
        log_event(log_data, category='feeding')

def control_local_valve(relay_port, action, valve_type, sio=None, app=None):
    """
    Control a local valve (feed_water or fresh_water) using the relay service.
    """
    try:
        if action == 'on':
            turn_on_relay(relay_port)
        else:
            turn_off_relay(relay_port)
        log_mixing_feedback(f"{valve_type} valve (port {relay_port}) turned {action}", status='success', sio=sio, app=app)
        return True
    except Exception as e:
        log_mixing_feedback(f"Failed to turn {action} {valve_type} valve (port {relay_port}): {str(e)}", status='error', sio=sio, app=app)
        from app import send_notification
        send_notification(f"Failed to turn {action} {valve_type} valve (port {relay_port}): {str(e)}")
        return False

def control_feed_pump(action, sio=None, app=None):
    """
    Control the feed pump based on its configuration (IO or Shelly).
    """
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    pump_type = feed_pump.get('type')
    io_number = feed_pump.get('io_number')
    log_mixing_feedback(f"Attempting to control feed pump: action={action}, pump_type={pump_type}, io_number={io_number}", status='debug', sio=sio, app=app)
    try:
        if pump_type == 'io' and io_number:
            state = 1 if action == 'on' else 0
            success = pump_control(io_number=io_number, pump_type=pump_type, state=state)
            if success:
                log_mixing_feedback(f"Feed pump (IO {io_number}) turned {action}", status='success', sio=sio, app=app)
                return True
            else:
                log_mixing_feedback(f"Failed to turn {action} feed pump (IO {io_number})", status='error', sio=sio, app=app)
                from app import send_notification
                send_notification(f"Failed to turn {action} feed pump (IO {io_number})")
                return False
        else:
            log_mixing_feedback(f"Invalid feed pump configuration", status='error', sio=sio, app=app)
            from app import send_notification
            send_notification(f"Invalid feed pump configuration")
            return False
    except Exception as e:
        log_mixing_feedback(f"Failed to turn {action} feed pump: {str(e)}", status='error', sio=sio, app=app)
        from app import send_notification
        send_notification(f"Failed to turn {action} feed pump: {str(e)}")
        return False

def wait_for_full_sensor(plant_ip, initial_triggered, expected_triggered, timeout=600, sio=None, app=None):
    """
    Wait for the full sensor to reach the expected triggered state with a state change.
    """
    with app.app_context():
        with app.config['plant_lock']:
            plant_data = app.config['plant_data']
            water_level = plant_data.get(plant_ip, {}).get('water_level', {})
            sensor_label = water_level.get('sensor1', {}).get('label', 'sensor1')
        log_mixing_feedback(f"Waiting for full sensor {sensor_label} to change from triggered={initial_triggered} to {expected_triggered} for {plant_ip}", status='info', sio=sio, app=app)
    
    start_time = time.time()
    counter = 0
    state_changed = False
    while time.time() - start_time < timeout:
        if not app.config.get('feeding_sequence_active', False):
            log_mixing_feedback(f"Feeding sequence ended during full sensor wait for {plant_ip}", status='info', sio=sio, app=app)
            return False
        with app.app_context():
            with app.config['plant_lock']:
                plant_data = app.config['plant_data']
                current_triggered = plant_data.get(plant_ip, {}).get('water_level', {}).get('sensor1', {}).get('triggered', initial_triggered)
            if current_triggered == expected_triggered:
                if initial_triggered != expected_triggered or state_changed:
                    log_mixing_feedback(f"Full sensor {sensor_label} reached expected state (triggered={expected_triggered}) for {plant_ip}", status='success', sio=sio, app=app)
                    return True
                state_changed = True  # Mark as changed if initial == expected
            if counter % 5 == 0:
                log_mixing_feedback(f"Current full sensor status for {plant_ip}: triggered={current_triggered}", status='info', sio=sio, app=app)
        eventlet.sleep(1)
        counter += 1
    log_mixing_feedback(f"Timeout waiting for full sensor {sensor_label} to change to triggered={expected_triggered} for {plant_ip}", status='error', sio=sio, app=app)
    from app import send_notification
    send_notification(f"Timeout waiting for full sensor {sensor_label} to change to triggered={expected_triggered} for {plant_ip}")
    return False

def monitor_feed_mixing(sio=None, app=None):
    """
    Monitor if feeding is active and control mixing of feed and fresh water.
    Runs in a separate thread.
    """
    global stop_mixing_flag
    if not app or not sio:
        print("[ERROR] Flask app or SocketIO instance not provided to monitor_feed_mixing")
        return
    with app.app_context():
        log_mixing_feedback("Monitor feed mixing thread started", status='info', sio=sio, app=app)
        log_mixing_feedback(f"Module context: {__name__}", status='debug', sio=sio, app=app)
    while True:
        try:
            with app.app_context():
                feeding_active = app.config.get('feeding_sequence_active', False)
                log_mixing_feedback(f"Checking feeding_sequence_active: {feeding_active}", status='debug', sio=sio, app=app)
            if not feeding_active:
                with app.app_context():
                    if app.config['debug_states'].get('feeding', False):
                        log_mixing_feedback("Feeding sequence not active, waiting", status='info', sio=sio, app=app)
                # Cleanup any active valves or pump
                settings = load_settings()
                relay_ports = settings.get('relay_ports', {})
                feed_valve_port = relay_ports.get('feed_water')
                fresh_valve_port = relay_ports.get('fresh_water')
                if feed_valve_port:
                    control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio, app=app)
                if fresh_valve_port:
                    control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio, app=app)
                control_feed_pump('off', sio=sio, app=app)
                eventlet.sleep(1)
                continue

            with app.app_context():
                log_mixing_feedback(f"Feeding sequence active detected, proceeding", status='debug', sio=sio, app=app)

            if stop_mixing_flag:
                with app.app_context():
                    log_mixing_feedback("Stop mixing flag set, resetting", status='info', sio=sio, app=app)
                stop_mixing_flag = False
                eventlet.sleep(1)
                continue

            settings = load_settings()
            with app.app_context():
                log_mixing_feedback(f"Loaded settings: nutrient_concentration={settings.get('nutrient_concentration', 1)}, relay_ports={settings.get('relay_ports', {})}", status='debug', sio=sio, app=app)
            nutrient_concentration = settings.get('nutrient_concentration', 1)
            relay_ports = settings.get('relay_ports', {})
            feed_valve_port = relay_ports.get('feed_water')
            fresh_valve_port = relay_ports.get('fresh_water')

            if not all([feed_valve_port, fresh_valve_port]):
                with app.app_context():
                    log_mixing_feedback("Missing feed or fresh valve port configuration", status='error', sio=sio, app=app)
                    from app import send_notification
                    send_notification("Missing feed or fresh valve port configuration")
                eventlet.sleep(1)
                continue

            with app.app_context():
                with app.config['plant_lock']:
                    plant_data = app.config['plant_data']
                    log_mixing_feedback(f"Plant data keys: {list(plant_data.keys())}", status='debug', sio=sio, app=app)
                    if not plant_data:
                        log_mixing_feedback("No plant data available during active feeding, waiting", status='error', sio=sio, app=app)
                        eventlet.sleep(1)
                        continue
                    try:
                        plant_ip = list(plant_data.keys())[0]  # Adjust for multiple plants
                        log_mixing_feedback(f"Selected plant_ip: {plant_ip}", status='debug', sio=sio, app=app)
                    except IndexError:
                        log_mixing_feedback("No plants in plant_data during active feeding, waiting", status='error', sio=sio, app=app)
                        eventlet.sleep(1)
                        continue
                    system_volume = plant_data[plant_ip]['settings'].get('system_volume', 5.5)
                    system_name = plant_data[plant_ip].get('system_name', plant_ip)
                    water_level = plant_data[plant_ip].get('water_level', {})
                    initial_full_sensor_triggered = water_level.get('sensor1', {}).get('triggered', True)
                    log_mixing_feedback(f"System details: name={system_name}, volume={system_volume}, initial_full_sensor_triggered={initial_full_sensor_triggered}", status='debug', sio=sio, app=app)

            with app.app_context():
                log_mixing_feedback(f"Feeding active, starting mixing monitoring for {system_name}", status='info', sio=sio, app=app)

            if nutrient_concentration == 0:
                target_nutrient = 0
                target_fresh = system_volume
                with app.app_context():
                    log_mixing_feedback(f"Using only fresh water for {system_name} (nutrient_concentration=0, target volume {system_volume} Gal)", status='info', sio=sio, app=app)
            else:
                total_parts = nutrient_concentration + 1
                target_nutrient = system_volume / total_parts
                target_fresh = system_volume - target_nutrient
                with app.app_context():
                    log_mixing_feedback(f"Target for {system_name}: {target_nutrient:.2f} Gal nutrient, {target_fresh:.2f} Gal fresh water (ratio {nutrient_concentration}:1)", status='info', sio=sio, app=app)

            nutrient_volume = 0
            fresh_volume = 0
            feed_valve_on = False
            fresh_valve_on = False
            pump_on = False
            counter = 0

            with app.app_context():
                log_mixing_feedback(f"Starting valve and pump control for {system_name}", status='debug', sio=sio, app=app)

            # Start mixing: Turn on valves and pump based on concentration
            if nutrient_concentration > 0:
                if control_local_valve(feed_valve_port, 'on', 'Feed', sio=sio, app=app):
                    feed_valve_on = True
                    if control_feed_pump('on', sio=sio, app=app):
                        pump_on = True
                else:
                    with app.app_context():
                        log_mixing_feedback(f"Failed to start feed valve for {system_name}, aborting mixing", status='error', sio=sio, app=app)
                        from app import send_notification
                        send_notification(f"Failed to start feed valve for {system_name}")
                    eventlet.sleep(1)
                    continue
            if control_local_valve(fresh_valve_port, 'on', 'Fresh', sio=sio, app=app):
                fresh_valve_on = True
            else:
                with app.app_context():
                    log_mixing_feedback(f"Failed to start fresh valve for {system_name}, aborting mixing", status='error', sio=sio, app=app)
                    from app import send_notification
                    send_notification(f"Failed to start fresh valve for {system_name}")
                if feed_valve_on:
                    control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio, app=app)
                if pump_on:
                    control_feed_pump('off', sio=sio, app=app)
                eventlet.sleep(1)
                continue

            with app.app_context():
                log_mixing_feedback(f"Entered mixing loop for {system_name}, feed_valve_on={feed_valve_on}, fresh_valve_on={fresh_valve_on}, pump_on={pump_on}", status='debug', sio=sio, app=app)

            # Wait for full sensor to transition from True to False
            if not wait_for_full_sensor(plant_ip, initial_full_sensor_triggered, False, timeout=600, sio=sio, app=app):
                with app.app_context():
                    log_mixing_feedback(f"Failed to detect full sensor transition for {system_name}, stopping mixing", status='error', sio=sio, app=app)
                    from app import send_notification
                    send_notification(f"Failed to detect full sensor transition for {system_name}")
                if feed_valve_on:
                    control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio, app=app)
                if pump_on:
                    control_feed_pump('off', sio=sio, app=app)
                if fresh_valve_on:
                    control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio, app=app)
                eventlet.sleep(1)
                continue

            while app.config.get('feeding_sequence_active', False) and not stop_mixing_flag:
                nutrient_volume = get_feed_total_volume() or 0
                fresh_volume = get_fresh_total_volume() or 0
                total_volume = nutrient_volume + fresh_volume

                with app.app_context():
                    log_mixing_feedback(f"Mixing loop iteration {counter}: nutrient={nutrient_volume:.2f}, fresh={fresh_volume:.2f}, total={total_volume:.2f}", status='debug', sio=sio, app=app)

                if total_volume >= system_volume - 0.01:
                    with app.app_context():
                        log_mixing_feedback(f"Total volume reached for {system_name} ({total_volume:.2f} Gal), stopping mixing", status='info', sio=sio, app=app)
                    break

                with app.app_context():
                    with app.config['plant_lock']:
                        water_level = plant_data.get(plant_ip, {}).get('water_level', {})
                        full_sensor_triggered = water_level.get('sensor1', {}).get('triggered', True)
                        log_mixing_feedback(f"Full sensor check: triggered={full_sensor_triggered}", status='debug', sio=sio, app=app)

                if not full_sensor_triggered:
                    with app.app_context():
                        log_mixing_feedback(f"Full sensor triggered for {system_name}, stopping mixing", status='success', sio=sio, app=app)
                    break

                if counter % 5 == 0:
                    with app.app_context():
                        if app.config['debug_states'].get('feed-flow', False):
                            feed_flow_rate = get_latest_feed_flow_rate() or 0
                            log_mixing_feedback(f"Feed flow for {system_name}: {feed_flow_rate:.2f} Gal/min, total: {nutrient_volume:.2f} Gal", status='info', sio=sio, app=app)
                        if app.config['debug_states'].get('fresh-flow', False):
                            fresh_flow_rate = get_latest_fresh_flow_rate() or 0
                            log_mixing_feedback(f"Fresh flow for {system_name}: {fresh_flow_rate:.2f} Gal/min, total: {fresh_volume:.2f} Gal", status='info', sio=sio, app=app)

                if nutrient_concentration > 0:
                    current_ratio = fresh_volume / nutrient_volume if nutrient_volume > 0 else float('inf')
                    with app.app_context():
                        log_mixing_feedback(f"Ratio check: current_ratio={current_ratio:.2f}, nutrient_concentration={nutrient_concentration}", status='debug', sio=sio, app=app)
                    if current_ratio < nutrient_concentration and fresh_valve_on:
                        with app.app_context():
                            log_mixing_feedback(f"Pausing fresh valve for {system_name} (current ratio {current_ratio:.2f} < {nutrient_concentration})", status='info', sio=sio, app=app)
                        control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio, app=app)
                        fresh_valve_on = False
                    elif current_ratio > nutrient_concentration and not fresh_valve_on:
                        with app.app_context():
                            log_mixing_feedback(f"Resuming fresh valve for {system_name} (current ratio {current_ratio:.2f} > {nutrient_concentration})", status='info', sio=sio, app=app)
                        if control_local_valve(fresh_valve_port, 'on', 'Fresh', sio=sio, app=app):
                            fresh_valve_on = True

                    if nutrient_volume >= target_nutrient and feed_valve_on:
                        with app.app_context():
                            log_mixing_feedback(f"Nutrient target reached for {system_name} ({nutrient_volume:.2f} Gal), stopping feed valve and pump", status='info', sio=sio, app=app)
                        control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio, app=app)
                        control_feed_pump('off', sio=sio, app=app)
                        feed_valve_on = False
                        pump_on = False

                if fresh_volume >= target_fresh and fresh_valve_on:
                    with app.app_context():
                        log_mixing_feedback(f"Fresh water target reached for {system_name} ({fresh_volume:.2f} Gal), stopping fresh valve", status='info', sio=sio, app=app)
                    control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio, app=app)
                    fresh_valve_on = False

                counter += 1
                eventlet.sleep(1)

            if feed_valve_on:
                control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio, app=app)
            if pump_on:
                control_feed_pump('off', sio=sio, app=app)
            if fresh_valve_on:
                control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio, app=app)

            with app.app_context():
                log_mixing_feedback(f"Mixing completed or stopped for {system_name}", status='success', sio=sio, app=app)

        except Exception as e:
            with app.app_context():
                log_mixing_feedback(f"Error in mixing loop: {str(e)}", status='error', sio=sio, app=app)
                from app import send_notification
                send_notification(f"Error in mixing loop: {str(e)}")
            # Cleanup on error
            settings = load_settings()
            relay_ports = settings.get('relay_ports', {})
            feed_valve_port = relay_ports.get('feed_water')
            fresh_valve_port = relay_ports.get('fresh_water')
            if feed_valve_port:
                control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio, app=app)
            if fresh_valve_port:
                control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio, app=app)
            control_feed_pump('off', sio=sio, app=app)
            eventlet.sleep(1)