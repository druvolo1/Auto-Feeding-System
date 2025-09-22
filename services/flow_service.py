import RPi.GPIO as GPIO
import time
from threading import Lock

FLOW_PIN = 18  # BCM pin
CALIBRATION_FACTOR = 7.5  # Adjust for your sensor

latest_flow = None
flow_lock = Lock()
pulse_count = 0

def pulse_callback(channel):
    global pulse_count
    pulse_count += 1
    print(f"[DEBUG] Pulse detected on pin {channel}! Total pulses this second: {pulse_count}")  # NEW: Log each pulse

def flow_reader():
    global pulse_count
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(FLOW_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.add_event_detect(FLOW_PIN, GPIO.RISING, callback=pulse_callback, bouncetime=10)  # NEW: Added debounce

    print("[DEBUG] GPIO setup complete. Waiting for pulses...")  # NEW: Confirm setup

    while True:
        pulse_count = 0
        time.sleep(1)
        flow_rate = (pulse_count / CALIBRATION_FACTOR) / 60  # L/min
        print(f"[DEBUG] Pulses in last second: {pulse_count}, Calculated flow: {flow_rate} L/min")  # NEW: Log every second
        with flow_lock:
            global latest_flow
            latest_flow = flow_rate

def get_latest_flow_rate():
    with flow_lock:
        return latest_flow