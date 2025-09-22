import json
import os

SETTINGS_FILE = os.path.join(os.getcwd(), "data", "settings.json")

# Ensure the settings file exists with default values
if not os.path.exists(SETTINGS_FILE):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    default_settings = {
        "additional_plants": [],
        "calibration_factors": {
            "fresh": 28.390575,
            "feed": 28.390575,
            "drain": 28.390575
        },
        "usb_roles": {
            "valve_relay": None
        },
        "relay_ports": {
            "feed_water": 1,
            "fresh_water": 2
        }
        # Add other default settings as needed
    }
    with open(SETTINGS_FILE, "w") as f:
        json.dump(default_settings, f, indent=4)

def load_settings():
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f)

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)