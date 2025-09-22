from flask import Blueprint, jsonify, request
import json
import os
from app import reload_event  # Import the event

settings_blueprint = Blueprint('settings', __name__)

SETTINGS_FILE = os.path.join(os.getcwd(), "data", "settings.json")

# Ensure the settings file exists with default values
if not os.path.exists(SETTINGS_FILE):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump({
            "additional_plants": []
            # Add other default settings as needed
        }, f, indent=4)

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

@settings_blueprint.route('', methods=['GET'])
def get_settings():
    settings = load_settings()
    return jsonify(settings)

@settings_blueprint.route('', methods=['POST'])
def update_settings():
    data = request.get_json() or {}
    settings = load_settings()

    plants_changed = 'additional_plants' in data
    if plants_changed:
        settings['additional_plants'] = settings.get('additional_plants', []) + data['additional_plants']

    # Handle other settings updates as needed

    save_settings(settings)
    
    if plants_changed:
        print("[DEBUG] Triggered reload_event.set() for add")
        reload_event.set()  # Trigger reload

    return jsonify({"status": "success", "settings": settings})

@settings_blueprint.route('/remove_plant', methods=['POST'])
def remove_plant():
    data = request.get_json() or {}
    index = data.get('index')
    if index is None:
        return jsonify({"status": "failure", "error": "No index provided"}), 400

    settings = load_settings()
    if 'additional_plants' in settings and 0 <= index < len(settings['additional_plants']):
        del settings['additional_plants'][index]
        save_settings(settings)
        print("[DEBUG] Triggered reload_event.set() for remove")
        reload_event.set()  # Trigger reload
        return jsonify({"status": "success", "settings": settings})
    else:
        return jsonify({"status": "failure", "error": "Invalid index"}), 400

@settings_blueprint.route('/settings')
def settings_page():
    return render_template('settings.html')