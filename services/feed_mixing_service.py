```python
import eventlet
import time
from flask import current_app
from datetime import datetime
from services.valve_relay_service import control_relay
from services.feed_pump_service import turn_on_feed_pump, turn_off_feed_pump
from utils.settings_utils import load_settings
from .log_service import log_event
from .feeding_service import feeding_sequence_active  # Import to monitor feeding status
from services.feed_flow_service import get_total_volume as get_feed_total_volume
from services.fresh_flow_service import get_total_volume as get_fresh_total_volume

# Global flag for mixing
stop_mixing_flag = False

def log_mixing_feedback(message, status='info', sio=None):
    """
    Log mixing feedback to both the UI (via SocketIO) and feeding.jsonl.
    """
    sio = sio or current_app.extensions.get('socketio')
    if not sio:
        print(f"[WARNING] SocketIO not available for logging: {message}")
        return
    log_data = {
        'event_type': 'mixing_feedback',
        'message': message,
        'status': status,
        'timestamp': datetime.now().isoformat()
    }
    
    sio.emit('feeding_feedback', log_data, namespace='/status')
    log_event(log_data, category='feeding')

def control_local_valve(relay_port, action, valve_type, sio=None):
    """
    Control a local valve (feed_water or fresh_water) using the relay service.
    """
    try:
        control_relay(relay_port, action == 'on')
        log_mixing_feedback(f"{valve_type} valve (port {relay_port}) turned {action}", status='success', sio=sio)
        return True
    except Exception as e:
        log_mixing_feedback(f"Failed to turn {action} {valve_type} valve (port {relay_port}): {str(e)}", status='error', sio=sio)
        from app import send_notification
        send_notification(f"Failed to turn {action} {valve_type} valve (port {relay_port}): {str(e)}")
        return False

def control_feed_pump(action, sio=None):
    """
    Control the feed pump based on its configuration (IO or Shelly).
    """
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    pump_type = feed_pump.get('type')
    try:
        if pump_type == 'io' and feed_pump.get('io_number'):
            if action == 'on':
                turn_on_feed_pump()
            else:
                turn_off_feed_pump()
            log_mixing_feedback(f"Feed pump (IO {feed_pump['io_number']}) turned {action}", status='success', sio=sio)
            return True
        # Add Shelly support if needed
        else:
            log_mixing_feedback(f"Invalid feed pump configuration", status='error', sio=sio)
            from app import send_notification
            send_notification(f"Invalid feed pump configuration")
            return False
    except Exception as e:
        log_mixing_feedback(f"Failed to turn {action} feed pump: {str(e)}", status='error', sio=sio)
        from app import send_notification
        send_notification(f"Failed to turn {action} feed pump: {str(e)}")
        return False

def monitor_feed_mixing(sio=None):
    """
    Monitor if feeding is active and control mixing of feed and fresh water.
    Runs in a separate thread.
    """
    global stop_mixing_flag
    while True:
        if feeding_sequence_active:
            if stop_mixing_flag:
                stop_mixing_flag = False
                continue

            settings = load_settings()
            nutrient_concentration = settings.get('nutrient_concentration', 1)
            relay_ports = settings.get('relay_ports', {})
            feed_valve_port = relay_ports.get('feed_water')
            fresh_valve_port = relay_ports.get('fresh_water')

            if not all([feed_valve_port, fresh_valve_port]):
                log_mixing_feedback(f"Missing feed or fresh valve port configuration", status='error', sio=sio)
                from app import send_notification
                send_notification(f"Missing feed or fresh valve port configuration")
                eventlet.sleep(1)
                continue

            # Get current plant data (assuming single plant for simplicity; adjust for multiple)
            with current_app.config['plant_lock']:
                plant_data = current_app.config['plant_data']
                # Example: Get first plant's data; adjust for multiple plants
                if not plant_data:
                    eventlet.sleep(1)
                    continue
                plant_ip = list(plant_data.keys())[0]  # Adjust to handle multiple
                system_volume = plant_data[plant_ip]['settings'].get('system_volume', 5.5)
                system_name = plant_data[plant_ip].get('system_name', plant_ip)
                water_level = plant_data[plant_ip].get('water_level', {})
                full_sensor_triggered = water_level.get('sensor1', {}).get('triggered', True)  # Assuming sensor1 is Full

            log_mixing_feedback(f"Feeding active, starting mixing monitoring for {system_name}", status='info', sio=sio)

            # Calculate targets: Handle nutrient_concentration = 0
            if nutrient_concentration == 0:
                target_nutrient = 0
                target_fresh = system_volume
                log_mixing_feedback(f"Using only fresh water for {system_name} (nutrient_concentration=0, target volume {system_volume} Gal)", status='info', sio=sio)
            else:
                total_parts = nutrient_concentration + 1
                target_nutrient = system_volume / total_parts
                target_fresh = system_volume - target_nutrient
                log_mixing_feedback(f"Target for {system_name}: {target_nutrient:.2f} Gal nutrient, {target_fresh:.2f} Gal fresh water (ratio {nutrient_concentration}:1)", status='info', sio=sio)

            nutrient_volume = 0
            fresh_volume = 0
            feed_valve_on = False
            fresh_valve_on = False
            pump_on = False

            # Start mixing: Turn on valves and pump based on concentration
            if nutrient_concentration > 0:
                if control_local_valve(feed_valve_port, 'on', 'Feed', sio=sio):
                    feed_valve_on = True
                    if control_feed_pump('on', sio=sio):
                        pump_on = True
            if control_local_valve(fresh_valve_port, 'on', 'Fresh', sio=sio):
                fresh_valve_on = True

            while feeding_sequence_active and not stop_mixing_flag:
                # Update volumes
                nutrient_volume = get_feed_total_volume() or 0
                fresh_volume = get_fresh_total_volume() or 0
                total_volume = nutrient_volume + fresh_volume

                # Check remote full sensor
                with current_app.config['plant_lock']:
                    water_level = plant_data.get(plant_ip, {}).get('water_level', {})
                    full_sensor_triggered = water_level.get('sensor1', {}).get('triggered', True)

                if not full_sensor_triggered:  # False means full
                    log_mixing_feedback(f"Full sensor triggered for {system_name}, stopping mixing", status='success', sio=sio)
                    break

                if total_volume >= system_volume:
                    log_mixing_feedback(f"Total volume reached for {system_name} ({total_volume:.2f} Gal), stopping mixing", status='info', sio=sio)
                    break

                # Adjust for ratio if nutrient_concentration > 0
                if nutrient_concentration > 0:
                    current_ratio = fresh_volume / nutrient_volume if nutrient_volume > 0 else float('inf')
                    if current_ratio < nutrient_concentration and fresh_valve_on:
                        log_mixing_feedback(f"Pausing fresh valve for {system_name} (current ratio {current_ratio:.2f} < {nutrient_concentration})", status='info', sio=sio)
                        control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio)
                        fresh_valve_on = False
                    elif current_ratio > nutrient_concentration and not fresh_valve_on:
                        log_mixing_feedback(f"Resuming fresh valve for {system_name} (current ratio {current_ratio:.2f} > {nutrient_concentration})", status='info', sio=sio)
                        control_local_valve(fresh_valve_port, 'on', 'Fresh', sio=sio)
                        fresh_valve_on = True

                    if nutrient_volume >= target_nutrient and feed_valve_on:
                        log_mixing_feedback(f"Nutrient target reached for {system_name} ({nutrient_volume:.2f} Gal), stopping feed valve and pump", status='info', sio=sio)
                        control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio)
                        control_feed_pump('off', sio=sio)
                        feed_valve_on = False
                        pump_on = False

                if fresh_volume >= target_fresh and fresh_valve_on:
                    log_mixing_feedback(f"Fresh water target reached for {system_name} ({fresh_volume:.2f} Gal), stopping fresh valve", status='info', sio=sio)
                    control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio)
                    fresh_valve_on = False

                eventlet.sleep(1)

            # Cleanup
            if feed_valve_on:
                control_local_valve(feed_valve_port, 'off', 'Feed', sio=sio)
            if pump_on:
                control_feed_pump('off', sio=sio)
            if fresh_valve_on:
                control_local_valve(fresh_valve_port, 'off', 'Fresh', sio=sio)

            log_mixing_feedback(f"Mixing completed or stopped for {system_name}", status='success', sio=sio)
        else:
            eventlet.sleep(1)  # Wait if not feeding