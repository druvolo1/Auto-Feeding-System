from flask import Blueprint, jsonify, request, current_app
from services.log_service import log_event

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
        'status': status
    }
    if plant_ip:
        log_data['plant_ip'] = plant_ip
    
    # Emit to UI
    socketio.emit('feeding_feedback', log_data, namespace='/status')
    
    # Log to feeding.jsonl
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

@feeding_blueprint.route('/status', methods=['GET'])
def get_feeding_status():
    return jsonify({"status": "not_implemented"})