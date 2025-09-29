import time
import requests
from flask import current_app
from utils.settings_utils import load_settings
from services.feed_flow_service import get_total_volume as get_feed_total_volume
from services.log_service import log_event
from .feeding_service import log_feeding_feedback, stop_feeding_flag, send_notification
from .feed_pump_service import control_feed_pump
import eventlet

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

def monitor_feed_mixing(socketio, app):
    """
    Background monitor that runs continuously to handle feed mixing during the fill phase.
    """
    mixed = False  # Flag to track if mixing has started for the current fill phase
    components_off = False  # Flag to track if components have been turned off
    last_processed_plant = None  # Track the last plant processed
    mixing_completed = False  # Flag to track if mixing has completed for the current plant

    while True:
        with app.app_context():  # Create application context
            if stop_feeding_flag:
                # Ensure components are off if sequence is stopped
                if not components_off:
                    settings = load_settings()
                    feed_pump = settings.get('feed_pump', {})
                    io_number = feed_pump.get('io_number')
                    pump_type = feed_pump.get('type', 'io')
                    feed_relay = settings.get('relay_ports', {}).get('feed_water')
                    fresh_relay = settings.get('relay_ports', {}).get('fresh_water')
                    control_feed_pump(io_number=io_number, pump_type=pump_type, state=0, sio=socketio)
                    if feed_relay:
                        control_local_relay(feed_relay, 'off', socketio, None)
                    if fresh_relay:
                        control_local_relay(fresh_relay, 'off', socketio, None)
                    log_feeding_feedback("Feed mixing stopped due to feeding sequence interruption, turned off pump and relays", status='info', sio=socketio)
                    components_off = True
                    mixed = False
                    mixing_completed = False
                    last_processed_plant = None
                eventlet.sleep(0.1)  # Longer sleep to reduce race conditions
                continue

            phase = app.config.get('current_feeding_phase', 'idle')
            plant_ip = app.config.get('current_plant_ip')
            use_feed = app.config.get('use_feed', True)

            # Reset flags when moving to a new plant or phase changes
            if plant_ip != last_processed_plant or phase != 'fill':
                mixed = False
                components_off = False
                mixing_completed = False
                last_processed_plant = plant_ip
                #log_feeding_feedback(f"Reset mixing state for new plant {plant_ip} or phase change to {phase}", plant_ip, 'debug', socketio)

            if phase == 'fill' and plant_ip and not mixed and use_feed and not mixing_completed:
                # Get system_volume from plant data
                with app.config['plant_lock']:
                    system_volume = app.config['plant_data'].get(plant_ip, {}).get('settings', {}).get('system_volume', 0)
                if system_volume == 0 or system_volume == 'N/A':
                    log_feeding_feedback(f"No valid system_volume for {plant_ip}, skipping mixing", plant_ip, 'warning', socketio)
                    mixing_completed = True  # Prevent re-attempting for this plant
                    eventlet.sleep(0.1)
                    continue

                # Calculate target feed volume
                settings = load_settings()
                ratio = settings.get('nutrient_concentration', 1)
                if ratio <= 0:
                    ratio = 1  # Prevent division by zero
                target_feed_volume = system_volume / (ratio + 1)
                log_feeding_feedback(f"Starting feed mixing for {plant_ip}, target feed volume: {target_feed_volume:.2f} Gal", plant_ip, 'info', socketio)

                # Get relay ports and pump settings
                feed_pump = settings.get('feed_pump', {})
                io_number = feed_pump.get('io_number')
                pump_type = feed_pump.get('type', 'io')
                feed_relay = settings.get('relay_ports', {}).get('feed_water')
                fresh_relay = settings.get('relay_ports', {}).get('fresh_water')

                # Turn on feed pump
                if not control_feed_pump(io_number=io_number, pump_type=pump_type, state=1, sio=socketio, plant_ip=plant_ip):
                    log_feeding_feedback(f"Failed to start feed pump for {plant_ip}, aborting mixing", plant_ip, 'error', socketio)
                    mixing_completed = True  # Prevent retries
                    eventlet.sleep(0.1)
                    continue

                # Turn on fresh relay (if defined)
                if fresh_relay and not control_local_relay(fresh_relay, 'on', socketio, plant_ip):
                    log_feeding_feedback(f"Failed to turn on fresh relay for {plant_ip}, aborting mixing", plant_ip, 'error', socketio)
                    control_feed_pump(io_number=io_number, pump_type=pump_type, state=0, sio=socketio, plant_ip=plant_ip)
                    mixing_completed = True  # Prevent retries
                    eventlet.sleep(0.1)
                    continue

                # Turn on feed relay
                if feed_relay and not control_local_relay(feed_relay, 'on', socketio, plant_ip):
                    log_feeding_feedback(f"No feed_water relay defined or failed to turn on, skipping feed relay control", plant_ip, 'warning', socketio)
                    control_feed_pump(io_number=io_number, pump_type=pump_type, state=0, sio=socketio, plant_ip=plant_ip)
                    if fresh_relay:
                        control_local_relay(fresh_relay, 'off', socketio, plant_ip)
                    mixing_completed = True  # Prevent retries
                    eventlet.sleep(0.1)
                    continue

                mixed = True
                components_off = False
                log_feeding_feedback(f"Feed mixing started for {plant_ip}, mixed={mixed}, components_off={components_off}, mixing_completed={mixing_completed}", plant_ip, 'debug', socketio)

                # Monitor feed total volume and phase
                while True:
                    with app.app_context():  # Refresh context inside loop
                        phase = app.config.get('current_feeding_phase', 'idle')
                        plant_ip = app.config.get('current_plant_ip')

                    if stop_feeding_flag or phase != 'fill':
                        # Turn off feed pump and relays on interruption or phase change
                        control_feed_pump(io_number=io_number, pump_type=pump_type, state=0, sio=socketio, plant_ip=plant_ip)
                        if feed_relay:
                            control_local_relay(feed_relay, 'off', socketio, plant_ip)
                        if fresh_relay:
                            control_local_relay(fresh_relay, 'off', socketio, plant_ip)
                        if stop_feeding_flag:
                            log_feeding_feedback(f"Feed mixing interrupted for {plant_ip}, turned off pump and relays", plant_ip, 'error', socketio)
                        else:
                            log_feeding_feedback(f"Fill phase completed for {plant_ip}, turned off feed pump and relays", plant_ip, 'info', socketio)
                        components_off = True
                        mixed = False
                        mixing_completed = True
                        last_processed_plant = plant_ip
                        break

                    feed_total = get_feed_total_volume()
                    if feed_total >= target_feed_volume and not components_off:
                        # Turn off feed pump and relays when target volume is reached
                        control_feed_pump(io_number=io_number, pump_type=pump_type, state=0, sio=socketio, plant_ip=plant_ip)
                        if feed_relay:
                            control_local_relay(feed_relay, 'off', socketio, plant_ip)
                        if fresh_relay:
                            control_local_relay(fresh_relay, 'off', socketio, plant_ip)
                        log_feeding_feedback(f"Target feed volume {target_feed_volume:.2f} Gal reached for {plant_ip} (actual: {feed_total:.2f} Gal), turned off feed pump and relays", plant_ip, 'success', socketio)
                        components_off = True
                        mixed = False
                        mixing_completed = True
                        last_processed_plant = plant_ip
                        break
                    eventlet.sleep(0.01)  # Fast check for volume and phase

            if mixed and phase != 'fill' and not components_off:
                # Ensure components are off if phase changes unexpectedly
                settings = load_settings()
                feed_pump = settings.get('feed_pump', {})
                io_number = feed_pump.get('io_number')
                pump_type = feed_pump.get('type', 'io')
                feed_relay = settings.get('relay_ports', {}).get('feed_water')
                fresh_relay = settings.get('relay_ports', {}).get('fresh_water')
                control_feed_pump(io_number=io_number, pump_type=pump_type, state=0, sio=socketio, plant_ip=plant_ip)
                if feed_relay:
                    control_local_relay(feed_relay, 'off', socketio, plant_ip)
                if fresh_relay:
                    control_local_relay(fresh_relay, 'off', socketio, plant_ip)
                log_feeding_feedback(f"Fill phase ended unexpectedly for {plant_ip}, turned off feed pump and relays", plant_ip, 'info', socketio)
                components_off = True
                mixed = False
                mixing_completed = True
                last_processed_plant = plant_ip
            elif mixed and phase != 'fill':
                log_feeding_feedback(f"Feed mixing cycle for {plant_ip} already cleaned up", plant_ip, 'debug', socketio)
                mixed = False
                mixing_completed = True
                last_processed_plant = plant_ip
            elif phase == 'fill' and plant_ip and not use_feed:
                log_feeding_feedback(f"Skipping feed mixing for {plant_ip} due to use_feed=False", plant_ip, 'debug', socketio)
            #elif phase == 'fill' and plant_ip and mixing_completed:
                #log_feeding_feedback(f"Skipping feed mixing for {plant_ip} as mixing already completed", plant_ip, 'debug', socketio)
        eventlet.sleep(0.1)  # Longer sleep to reduce race conditions