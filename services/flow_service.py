import RPi.GPIO as GPIO
import time
from threading import Lock

FLOW_PIN = 18  # BCM pin
CALIBRATION_FACTOR = 7.5  # Adjust for your sensor

latest_flow = None
flow_lock = Lock()

def flow_reader():
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(FLOW_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print("[DEBUG] GPIO setup complete on pin 18. Starting polling loop...")
    except Exception as e:
        print(f"[ERROR] GPIO setup failed: {e}")
        return  # Exit if setup fails (e.g., permissions)

    while True:
        try:
            pulse_count = 0
            last_state = GPIO.input(FLOW_PIN)
            start_time = time.time()

            while time.time() - start_time < 1:  # Poll for 1 second
                current_state = GPIO.input(FLOW_PIN)
                if current_state == 1 and last_state == 0:  # Detect rising edge
                    pulse_count += 1
                    print(f"[DEBUG] Pulse detected! Total in this second: {pulse_count}")
                last_state = current_state
                time.sleep(0.001)  # Poll every 1ms

            flow_rate = (pulse_count / CALIBRATION_FACTOR) / 60  # L/min
            print(f"[DEBUG] Pulses in last second: {pulse_count}, Calculated flow: {flow_rate} L/min")

            with flow_lock:
                global latest_flow
                latest_flow = flow_rate
        except Exception as e:
            print(f"[ERROR] Flow reader loop error: {e}")

def get_latest_flow_rate():
    with flow_lock:
        return latest_flow