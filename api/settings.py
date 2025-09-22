from flask import Blueprint, jsonify, request
import api.fresh_flow
import api.feed_flow
import api.drain_flow
from services.valve_relay_service import reinitialize_relay_service
from utils.settings_utils import load_settings, save_settings

settings_blueprint = Blueprint('settings', __name__)

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

    if 'calibration_factors' in data:
        settings['calibration_factors'] = data['calibration_factors']
        # Update running calibration factors in sensor modules
        api.fresh_flow.set_calibration_factor(settings['calibration_factors']['fresh'])
        api.feed_flow.set_calibration_factor(settings['calibration_factors']['feed'])
        api.drain_flow.set_calibration_factor(settings['calibration_factors']['drain'])

    if 'relay_ports' in data:
        settings['relay_ports'] = data['relay_ports']

    save_settings(settings)
    
    if plants_changed:
        from app import reload_event
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
        from app import reload_event
        print("[DEBUG] Triggered reload_event.set() for remove")
        reload_event.set()  # Trigger reload
        return jsonify({"status": "success", "settings": settings})
    else:
        return jsonify({"status": "failure", "error": "Invalid index"}), 400

@settings_blueprint.route('/usb_devices', methods=['GET'])
def list_usb_devices():
    import subprocess
    devices = []
    try:
        result = subprocess.check_output("ls /dev/serial/by-path", shell=True).decode().splitlines()
        devices = [{"device": f"/dev/serial/by-path/{dev}"} for dev in result]
    except Exception as e:
        print(f"Error listing USB devices: {e}")
    return jsonify(devices)

@settings_blueprint.route('/assign_usb', methods=['POST'])
def assign_usb_device():
    data = request.get_json() or {}
    role = data.get("role")
    device = data.get("device")

    if role != "valve_relay":
        return jsonify({"status": "failure", "error": "Invalid role"}), 400

    settings = load_settings()
    settings.setdefault("usb_roles", {})[role] = device  # Safely create dict if missing
    save_settings(settings)

    # Reinitialize the valve relay service if device changed
    reinitialize_relay_service()

    return jsonify({"status": "success", "usb_roles": settings["usb_roles"]})

@settings_blueprint.route('/settings')
def settings_page():
    return render_template('settings.html')