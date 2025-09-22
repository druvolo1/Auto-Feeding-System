import RPi.GPIO as GPIO
import time
from threading import Lock
from api.debug import debug_states  # Import for conditional debug

FLOW_PIN = 18  # BCM pin for fresh flow
CALIBRATION_FACTOR = 28.390575  # Pulses per gallon (original 7.5 per liter * 3.78541 liters/gallon)

latest_flow = None
total_volume = 0.0  # Accumulated total in gallons
flow_lock = Lock()

def flow_reader():
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(FLOW_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        if debug_states.get('fresh_flow', False):
            print("[DEBUG] Fresh GPIO setup complete on pin 18. Starting polling loop...")
    except Exception as e:
        print(f"[ERROR] Fresh GPIO setup failed: {e}")
        return

    while True:
        try:
            pulse_count = 0
            last_state = GPIO.input(FLOW_PIN)
            start_time = time.time()

            while time.time() - start_time < 1:
                current_state = GPIO.input(FLOW_PIN)
                if current_state == 1 and last_state == 0:
                    pulse_count += 1
                    if debug_states.get('fresh_flow', False):
                        print(f"[DEBUG] Fresh pulse detected! Total in this second: {pulse_count}")
                last_state = current_state
                time.sleep(0.001)

            flow_rate = pulse_count / CALIBRATION_FACTOR
            if debug_states.get('fresh_flow', False):
                print(f"[DEBUG] Fresh pulses in last second: {pulse_count}, Calculated flow: {flow_rate} gal/min")

            with flow_lock:
                global latest_flow, total_volume
                latest_flow = flow_rate
                total_volume += flow_rate / 60  # Accumulate (gal/min / 60 = gallons this second)
        except Exception as e:
            print(f"[ERROR] Fresh flow reader loop error: {e}")

def get_latest_flow_rate():
    with flow_lock:
        return latest_flow

def get_total_volume():
    with flow_lock:
        return total_volume

def reset_total():
    with flow_lock:
        global total_volume
        total_volume = 0.0
        if debug_states.get('fresh_flow', False):
            print("[DEBUG] Total volume reset to 0.0 gallons")