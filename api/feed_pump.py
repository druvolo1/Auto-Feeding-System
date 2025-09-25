from flask import Blueprint, jsonify, request
from utils.settings_utils import load_settings, save_settings
from services.feed_pump_service import control_feed_pump

feed_pump_blueprint = Blueprint('feed_pump', __name__)

@feed_pump_blueprint.route('/on', methods=['POST'])
def turn_on_feed_pump():
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    ip = feed_pump.get('ip')
    pump_type = feed_pump.get('type', 'kasa')  # Default to 'kasa' if not set

    if not ip:
        return jsonify({"status": "failure", "error": "Feed pump IP not configured"}), 400

    try:
        success = control_feed_pump(ip, pump_type, state=1)  # 1 for ON
        if success:
            return jsonify({"status": "success"})
        else:
            return jsonify({"status": "failure", "error": "Failed to turn on feed pump"}), 500
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

@feed_pump_blueprint.route('/off', methods=['POST'])
def turn_off_feed_pump():
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    ip = feed_pump.get('ip')
    pump_type = feed_pump.get('type', 'kasa')  # Default to 'kasa' if not set

    if not ip:
        return jsonify({"status": "failure", "error": "Feed pump IP not configured"}), 400

    try:
        success = control_feed_pump(ip, pump_type, state=0)  # 0 for OFF
        if success:
            return jsonify({"status": "success"})
        else:
            return jsonify({"status": "failure", "error": "Failed to turn off feed pump"}), 500
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

@feed_pump_blueprint.route('/status', methods=['GET'])
def get_feed_pump_status():
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    ip = feed_pump.get('ip')
    pump_type = feed_pump.get('type', 'kasa')  # Default to 'kasa' if not set

    if not ip:
        return jsonify({"status": "failure", "error": "Feed pump IP not configured"}), 400

    try:
        status = control_feed_pump(ip, pump_type, get_status=True)
        return jsonify({"status": "success", "state": status})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500