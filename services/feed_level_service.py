import RPi.GPIO as GPIO

PIN = 4  # Hardcoded GPIO pin 4, chosen as a general-purpose pin less commonly used for special functions

def setup_feed_level_sensor():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Assuming active low when reservoir is empty

setup_feed_level_sensor()

def get_feed_level():
    if GPIO.input(PIN) == GPIO.LOW:
        return 'Empty'
    else:
        return 'Full'