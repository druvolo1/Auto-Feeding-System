import time
import requests
from flask import current_app
from utils.settings_utils import load_settings
from services.feed_flow_service import get_total_volume as get_feed_total_volume
from services.log_service import log_event
from .feeding_service import log_feeding_feedback, stop_feeding_flag, send_notification

def control_local_relay(relay_id, action, sio=None, plant_ip=None, status='info'):
    """
    Control a local relay via the internal API endpoint.
    """
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

def control_feed_pump_api(action, sio=None, plant_ip=None):
    """
    Control the feed pump via the /api/feed_pump/{action} endpoint.
    action: 'on' or 'off'
    """
    url = f"http://127.0.0.1:8000/api/feed_pump/{action}"
    try:
        response = requests.post(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'success':
            log_feeding_feedback(f"Feed pump turned {action.upper()}", plant_ip, 'success', sio)
            return True
        else:
            log_feeding_feedback(f"Failed to turn {action} feed pump: {data.get('error')}", plant_ip, 'error', sio)
            send_notification(f"Failed to turn {action} feed pump: {data.get('error')}")
            return False
    except Exception as e:
        log_feeding_feedback(f"Error controlling feed pump via API ({action}): {str(e)}", plant_ip, 'error', sio)
        send_notification(f"Error controlling feed pump via API ({action}): {str(e)}")
        return False

def monitor_feed_mixing(socketio, app):
    """
    Background monitor that runs continuously to handle feed mixing during the fill phase.
    """
    mixed = False  # Flag to track if mixing has started for the current fill phase
    while True:
        with app.app_context():  # Create application context
            phase = app.config.get('current_feeding_phase', 'idle')
            plant_ip = app.config.get('current_plant_ip')
            if phase == 'fill' and plant_ip and not mixed:
                # Get system_volume from plant data
                with app.config['plant_lock']:
                    system_volume = app.config['plant_data'].get(plant_ip, {}).get('settings', {}).get('system_volume', 0)
                if system_volume == 0 or system_volume == 'N/A':
                    log_feeding_feedback(f"No valid system_volume for {plant_ip}, skipping mixing", plant_ip, 'warning', socketio)
                    time.sleep(1)
                    continue

                # Calculate target feed volume
                settings = load_settings()
                ratio = settings.get('nutrient_concentration', 1)
                if ratio <= 0:
                    ratio = 1  # Prevent division by zero
                target_feed_volume = system_volume / (ratio + 1)
                log_feeding_feedback(f"Starting feed mixing for {plant_ip}, target feed volume: {target_feed_volume:.2f} Gal", plant_ip, 'info', socketio)

                # Get relay ports
                feed_relay = settings.get('relay_ports', {}).get('feed_water')
                fresh_relay = settings.get('relay_ports', {}).get('fresh_water')

                # Turn on feed pump via API
                control_feed_pump_api('on', socketio, plant_ip)

                # Turn on fresh relay (if defined)
                if fresh_relay:
                    control_local_relay(fresh_relay, 'on', socketio, plant_ip)

                # Turn on feed relay
                if feed_relay:
                    control_local_relay(feed_relay, 'on', socketio, plant_ip)
                else:
                    log_feeding_feedback(f"No feed_water relay defined, skipping feed relay control", plant_ip, 'warning', socketio)

                mixed = True

                # Monitor feed total volume until target reached or phase changes
                while True:
                    with app.app_context():  # Refresh context inside loop for updated phase
                        phase = app.config.get('current_feeding_phase', 'idle')
                        plant_ip = app.config.get('current_plant_ip')

                    if stop_feeding_flag or phase != 'fill':
                        # Turn off feed pump and relays on interruption or phase change
                        control_feed_pump_api('off', socketio, plant_ip)
                        if feed_relay:
                            control_local_relay(feed_relay, 'off', socketio, plant_ip)
                        if fresh_relay:
                            control_local_relay(fresh_relay, 'off', socketio, plant_ip)
                        if stop_feeding_flag:
                            log_feeding_feedback(f"Feed mixing interrupted by user for {plant_ip}, turned off pump and relays", plant_ip, 'error', socketio)
                        else:
                            log_feeding_feedback(f"Fill phase completed for {plant_ip}, turned off feed pump and relays", plant_ip, 'info', socketio)
                        mixed = False
                        break

                    feed_total = get_feed_total_volume()
                    if feed_total >= target_feed_volume:
                        # Turn off feed pump and relays when target volume is reached
                        control_feed_pump_api('off', socketio, plant_ip)
                        if feed_relay:
                            control_local_relay(feed_relay, 'off', socketio, plant_ip)
                        if fresh_relay:
                            control_local_relay(fresh_relay, 'off', socketio, plant_ip)
                        log_feeding_feedback(f"Target feed volume {target_feed_volume:.2f} Gal reached for {plant_ip} (actual: {feed_total:.2f} Gal), turned off feed pump and relays", plant_ip, 'success', socketio)
                        break
                    time.sleep(0.5)  # Check more frequently

            if mixed and phase != 'fill':
                log_feeding_feedback(f"Feed mixing cycle for {plant_ip} already cleaned up or not needed", plant_ip, 'debug', socketio)
                mixed = False  # Safety reset
        time.sleep(0.5)  # Main loop sleep