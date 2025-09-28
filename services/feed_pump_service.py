import RPi.GPIO as GPIO
import requests
from utils.settings_utils import load_settings
from .feeding_service import log_feeding_feedback, send_notification

def control_feed_pump(io_number=None, pump_type='io', state=None, get_status=False, sio=None, plant_ip=None):
    """
    Control the feed pump or get its status.
    io_number: GPIO pin number for 'io' type, or None for 'shelly' type
    pump_type: 'io' for GPIO control, 'shelly' for network-based control
    state: 0 for off, 1 for on, None if getting status
    get_status: True to return pump status, False to control pump
    sio: SocketIO instance for logging
    plant_ip: IP of the plant for logging context
    """
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    pump_type = pump_type or feed_pump.get('type', 'io')

    if pump_type == 'io':
        io_number = io_number or feed_pump.get('io_number')
        if not io_number:
            log_feeding_feedback("Feed pump IO number not configured", plant_ip, 'error', sio)
            send_notification("Feed pump IO number not configured")
            raise ValueError("Feed pump IO number not configured")
    elif pump_type == 'shelly':
        ip = feed_pump.get('ip')
        if not ip:
            log_feeding_feedback("Feed pump IP not configured for Shelly", plant_ip, 'error', sio)
            send_notification("Feed pump IP not configured for Shelly")
            raise ValueError("Feed pump IP not configured for Shelly")
    else:
        log_feeding_feedback(f"Unsupported pump type: {pump_type}", plant_ip, 'error', sio)
        send_notification(f"Unsupported pump type: {pump_type}")
        raise ValueError("Unsupported pump type")

    try:
        if pump_type == 'io':
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(int(io_number), GPIO.OUT)

            if get_status:
                current_state = GPIO.input(int(io_number))
                log_feeding_feedback(f"Feed pump status for IO {io_number}: {'ON' if current_state else 'OFF'}", plant_ip, 'debug', sio)
                return current_state  # Active-high: 1=ON, 0=OFF

            if state not in [0, 1]:
                log_feeding_feedback(f"Invalid state {state} for feed pump control", plant_ip, 'error', sio)
                send_notification(f"Invalid state {state} for feed pump control")
                return False

            GPIO.output(int(io_number), state)  # Active-high: 1=ON, 0=OFF
            action = 'ON' if state == 1 else 'OFF'
            log_feeding_feedback(f"Feed pump turned {action} on IO {io_number}", plant_ip, 'success', sio)
            log_feeding_feedback(f"Feed pump turned {action}", plant_ip, 'success', sio)
            return True

        elif pump_type == 'shelly':
            if get_status:
                status_url = f"http://{ip}/relay/0"
                response = requests.get(status_url, timeout=5)
                response.raise_for_status()
                data = response.json()
                status = 1 if data.get('ison', False) else 0
                log_feeding_feedback(f"Feed pump status for Shelly at {ip}: {'ON' if status else 'OFF'}", plant_ip, 'debug', sio)
                return status

            if state not in [0, 1]:
                log_feeding_feedback(f"Invalid state {state} for feed pump control", plant_ip, 'error', sio)
                send_notification(f"Invalid state {state} for feed pump control")
                return False

            url = f"http://{ip}/relay/0?turn={'on' if state == 1 else 'off'}"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            action = 'ON' if state == 1 else 'OFF'
            log_feeding_feedback(f"Feed pump turned {action} on Shelly at {ip}", plant_ip, 'success', sio)
            log_feeding_feedback(f"Feed pump turned {action}", plant_ip, 'success', sio)
            return True

    except Exception as e:
        log_feeding_feedback(f"Error controlling feed pump ({pump_type}): {str(e)}", plant_ip, 'error', sio)
        send_notification(f"Error controlling feed pump ({pump_type}): {str(e)}")
        return False