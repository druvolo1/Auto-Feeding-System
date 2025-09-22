import RPi.GPIO as GPIO
import time
from threading import Lock

FLOW_PIN = 18  # BCM pin - PWM capable (change if needed)
CALIBRATION_FACTOR = 7.5  # Pulses per liter (adjust for your flow meter, e.g., YF-S201 is ~7.5)

latest_flow = None
flow_lock = Lock()
pulse_count = 0

def pulse_callback(channel):
    global pulse_count
    pulse_count += 1

def flow_reader():
    global pulse_count
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(FLOW_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Enable internal pull-up
    GPIO.add_event_detect(FLOW_PIN, GPIO.RISING, callback=pulse_callback)

    while True:
        pulse_count = 0
        time.sleep(1)  # Measure over 1 second
        flow_rate = (pulse_count / CALIBRATION_FACTOR) / 60  # Liters per minute
        with flow_lock:
            global latest_flow
            latest_flow = flow_rate

def get_latest_flow_rate():
    with flow_lock:
        return latest_flow