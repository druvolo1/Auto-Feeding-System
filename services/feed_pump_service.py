import RPi.GPIO as GPIO
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Global setup for GPIO (called once at module import)
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

def control_feed_pump(io_number=None, pump_type=None, state=None, get_status=False):
    """
    Control or query the state of a GPIO pin or Shelly smart plug.
    For now, only IO is implemented.
    Args:
        io_number (str): GPIO pin number (BCM) for IO control.
        pump_type (str): 'io' or 'shelly'.
        state (int, optional): 1 to turn on, 0 to turn off. Omit for status query.
        get_status (bool): If True, return the current state instead of setting it.
    Returns:
        bool or int: True/False for control success, 1/0 for status (on/off).
    """
    if pump_type != 'io':
        raise ValueError("Only IO support is implemented currently")

    if not io_number or not io_number.isdigit():
        logger.error("Invalid or missing IO number")
        raise ValueError("IO number must be a valid integer")

    pin = int(io_number)
    GPIO.setup(pin, GPIO.OUT)  # Configure as output

    try:
        if get_status:
            logger.debug(f"Querying status for GPIO pin {pin}")
            current_state = GPIO.input(pin)
            logger.debug(f"GPIO pin {pin} state: {current_state}")
            return current_state  # 1 = on, 0 = off

        logger.debug(f"Setting GPIO pin {pin} to state {state}")
        GPIO.output(pin, state)  # Set the pin state
        return True  # Assume success for now
    except GPIO.error as e:
        logger.error(f"GPIO error: {str(e)}")
        raise
    finally:
        # Cleanup is handled by the app if needed, but keep pin configured
        pass