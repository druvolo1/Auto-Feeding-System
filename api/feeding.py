from flask import Blueprint, jsonify, request
from app import plant_clients  # Reference global plant_clients dict

feeding_blueprint = Blueprint('feeding', __name__)

@feeding_blueprint.route('/start', methods=['POST'])
def start_feeding():
    data = request.get_json() or {}
    plant_ip = data.get('plant_ip')
    if not plant_ip or plant_ip not in plant_clients:
        return jsonify({"status": "failure", "error": "Invalid or disconnected plant"}), 400

    client = plant_clients[plant_ip]
    try:
        # Emit command to remote (assume remote listens for 'start_feeding')
        client.emit('start_feeding', namespace='/status')
        return jsonify({"status": "success", "message": f"Feeding started for {plant_ip}"})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

@feeding_blueprint.route('/stop', methods=['POST'])
def stop_feeding():
    data = request.get_json() or {}
    plant_ip = data.get('plant_ip')
    if not plant_ip or plant_ip not in plant_clients:
        return jsonify({"status": "failure", "error": "Invalid or disconnected plant"}), 400

    client = plant_clients[plant_ip]
    try:
        # Emit command to remote (assume remote listens for 'stop_feeding')
        client.emit('stop_feeding', namespace='/status')
        return jsonify({"status": "success", "message": f"Feeding stopped for {plant_ip}"})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

# Placeholder for future endpoints, e.g., get feeding status (could query remote directly if needed)
@feeding_blueprint.route('/status', methods=['GET'])
def get_feeding_status():
    # For now, return placeholder; expand later to aggregate from plant_data
    return jsonify({"status": "not_implemented"})