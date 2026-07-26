from flask import Blueprint, jsonify, request, current_app
from services.log_service import log_event
from services.feeding_service import (
    start_feeding_sequence,
    stop_feeding_sequence,
    get_live_allow_remote_feeding,
)
from utils.mdns_utils import standardize_host_ip
from utils.settings_utils import load_settings
from datetime import datetime
import time

feeding_blueprint = Blueprint('feeding', __name__)

def log_feeding_feedback(message, plant_ip=None, status='info'):
    """
    Log feeding feedback to both the UI (via SocketIO) and feeding.jsonl.
    
    Args:
        message (str): The feedback message to display and log.
        plant_ip (str, optional): The IP of the plant, if applicable.
        status (str): The status of the feedback ('info', 'success', 'error').
    """
    socketio = current_app.extensions['socketio']
    log_data = {
        'event_type': 'feeding_feedback',
        'message': message,
        'status': status,
        'timestamp': datetime.now().isoformat()  # Add timestamp
    }
    if plant_ip:
        log_data['plant_ip'] = plant_ip
    
    socketio.emit('feeding_feedback', log_data, namespace='/status')
    log_event(log_data, category='feeding')

@feeding_blueprint.route('/start', methods=['POST'])
def start_feeding():
    data = request.get_json() or {}
    plant_ip = data.get('plant_ip')
    plant_clients = current_app.config.get('plant_clients', {})
    if not plant_ip or plant_ip not in plant_clients:
        error_msg = "Invalid or disconnected plant"
        log_feeding_feedback(error_msg, plant_ip, status='error')
        return jsonify({"status": "failure", "error": error_msg}), 400

    client = plant_clients[plant_ip]
    try:
        client.emit('start_feeding', namespace='/status')
        log_feeding_feedback(f"Feeding started for {plant_ip}", plant_ip, status='success')
        return jsonify({"status": "success", "message": f"Feeding started for {plant_ip}"})
    except Exception as e:
        log_feeding_feedback(f"Failed to start feeding for {plant_ip}: {str(e)}", plant_ip, status='error')
        return jsonify({"status": "failure", "error": str(e)}), 500

@feeding_blueprint.route('/stop', methods=['POST'])
def stop_feeding():
    data = request.get_json() or {}
    plant_ip = data.get('plant_ip')
    plant_clients = current_app.config.get('plant_clients', {})
    if not plant_ip or plant_ip not in plant_clients:
        error_msg = "Invalid or disconnected plant"
        log_feeding_feedback(error_msg, plant_ip, status='error')
        return jsonify({"status": "failure", "error": error_msg}), 400

    client = plant_clients[plant_ip]
    try:
        client.emit('stop_feeding', namespace='/status')
        log_feeding_feedback(f"Feeding stopped for {plant_ip}", plant_ip, status='success')
        return jsonify({"status": "success", "message": f"Feeding stopped for {plant_ip}"})
    except Exception as e:
        log_feeding_feedback(f"Failed to stop feeding for {plant_ip}: {str(e)}", plant_ip, status='error')
        return jsonify({"status": "failure", "error": str(e)}), 500

@feeding_blueprint.route('/start_all', methods=['POST'])
def start_all_feeding():
    try:
        message = start_feeding_sequence()
        return jsonify({"status": "success", "message": message})
    except Exception as e:
        log_feeding_feedback(f"Failed to start feeding sequence: {str(e)}", status='error')
        return jsonify({"status": "failure", "error": str(e)}), 500

@feeding_blueprint.route('/stop_all', methods=['POST'])
def stop_all_feeding():
    try:
        message = stop_feeding_sequence()
        return jsonify({"status": "success", "message": message})
    except Exception as e:
        log_feeding_feedback(f"Failed to stop feeding sequence: {str(e)}", status='error')
        return jsonify({"status": "failure", "error": str(e)}), 500

@feeding_blueprint.route('/status', methods=['GET'])
def get_feeding_status():
    return jsonify({"status": "not_implemented"})

@feeding_blueprint.route('/preflight', methods=['GET'])
def feeding_preflight():
    """Report exactly which plants a Start All would feed, and why the rest are skipped.

    Reads each zone's permission flag live rather than trusting the socket cache.
    """
    stale_after = current_app.config.get('PLANT_STALE_AFTER', 35)
    plant_data = current_app.config.get('plant_data', {})
    plant_clients = current_app.config.get('plant_clients', {})
    plants = []

    for host in load_settings().get('additional_plants', []):
        last_update = (plant_data.get(host) or {}).get('last_update')
        age = None if not last_update else round(max(0.0, time.time() - last_update / 1000.0), 1)

        entry = {
            'host': host,
            'ip': standardize_host_ip(host),
            'socket_connected': bool(getattr(plant_clients.get(host), 'connected', False)),
            'socket_age_s': age,
            'allow_remote_feeding_live': None,
            'will_feed': False,
            'reason': None
        }

        allowed, error = get_live_allow_remote_feeding(host)
        if error:
            entry['reason'] = error
        else:
            entry['allow_remote_feeding_live'] = allowed
            if not allowed:
                entry['reason'] = 'remote_feeding_disabled'
            elif age is None:
                entry['reason'] = 'no_telemetry'
            elif age > stale_after:
                entry['reason'] = 'stale_telemetry'
            else:
                entry['will_feed'] = True
                entry['reason'] = 'ok'

        plants.append(entry)

    return jsonify({
        "status": "success",
        "stale_after_s": stale_after,
        "will_feed": [p['host'] for p in plants if p['will_feed']],
        "skipped": [{'host': p['host'], 'reason': p['reason']} for p in plants if not p['will_feed']],
        "plants": plants
    })