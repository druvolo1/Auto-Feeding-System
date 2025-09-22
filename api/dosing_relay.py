# File: api/dosing_relay.py

from flask import Blueprint, request, jsonify
from services.dosing_relay_service import turn_on_relay, turn_off_relay, get_relay_status

# Create Blueprint
dosing_relay_blueprint = Blueprint('dosing_relay', __name__)

# API Endpoint: Turn relay on
@dosing_relay_blueprint.route('/<int:relay_id>/on', methods=['POST'])
def relay_on(relay_id):
    try:
        turn_on_relay(relay_id)
        return jsonify({"status": "success", "relay_id": relay_id, "action": "on"})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

# API Endpoint: Turn relay off
@dosing_relay_blueprint.route('/<int:relay_id>/off', methods=['POST'])
def relay_off(relay_id):
    try:
        turn_off_relay(relay_id)
        return jsonify({"status": "success", "relay_id": relay_id, "action": "off"})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

# API Endpoint: Get relay status
@dosing_relay_blueprint.route('/<int:relay_id>/status', methods=['GET'])
def relay_status(relay_id):
    try:
        status = get_relay_status(relay_id)
        return jsonify({"status": "success", "relay_id": relay_id, "relay_status": status})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500