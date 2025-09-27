import RPi.GPIO as GPIO
import requests
from utils.settings_utils import load_settings
from .feeding_service import log_feeding_feedback, send_notification

def control_feed_pump(io_number=None, pump_type='io', state=None, get_status=False):
    """
    Control the feed pump (on/off) or get its status.
    state: 1 for ON, 0 for OFF, None for status check.
    pump_type: 'io' for GPIO-based pump, 'shelly' for Shelly-based pump.
    """
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    
    # Use provided io_number and pump_type or fall back to settings
    if pump_type == 'io':
        io_number = io_number or feed_pump.get('io_number')
        if not io_number:
            log_feeding_feedback("Feed pump IO number not configured", status='error')
            send_notification("Feed pump IO number not configured")
            raise ValueError("Feed pump IO number not configured")
    elif pump_type == 'shelly':
        ip = feed_pump.get('ip')
        if not ip:
            log_feeding_feedback("Feed pump IP not configured for Shelly", status='error')
            send_notification("Feed pump IP not configured for Shelly")
            raise ValueError("Feed pump IP not configured for Shelly")
    else:
        log_feeding_feedback(f"Unsupported pump type: {pump_type}", status='error')
        send_notification(f"Unsupported pump type: {pump_type}")
        raise ValueError("Unsupported pump type")

    try:
        if pump_type == 'io':
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(int(io_number), GPIO.OUT)

            if get_status:
                # Get current GPIO state (0=ON, 1=OFF assuming active-low logic)
                current_state = GPIO.input(int(io_number))
                return 1 if current_state == 0 else 0  # Convert to 1=ON, 0=OFF

            if state is None:
                log_feeding_feedback("No state provided for feed pump control", status='error')
                send_notification("No state provided for feed pump control")
                return False

            # Active-low logic: 0=ON, 1=OFF
            GPIO.output(int(io_number), 0 if state == 1 else 1)
            log_feeding_feedback(f"Feed pump turned {'ON' if state == 1 else 'OFF'} on IO {io_number}", status='success')
            return True

        elif pump_type == 'shelly':
            if get_status:
                status_url = f"http://{ip}/relay/0"
                response = requests.get(status_url, timeout=5)
                response.raise_for_status()
                data = response.json()
                return 1 if data.get('ison', False) else 0  # 1=ON, 0=OFF

            if state is None:
                log_feeding_feedback("No state provided for feed pump control", status='error')
                send_notification("No state provided for feed pump control")
                return False

            url = f"http://{ip}/relay/0?turn={'on' if state == 1 else 'off'}"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            log_feeding_feedback(f"Feed pump turned {'ON' if state == 1 else 'OFF'} on Shelly at {ip}", status='success')
            return True

    except Exception as e:
        log_feeding_feedback(f"Error controlling feed pump ({pump_type}): {str(e)}", status='error')
        send_notification(f"Error controlling feed pump ({pump_type}): {str(e)}")
        raise