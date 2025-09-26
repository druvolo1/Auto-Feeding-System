from flask import Blueprint, jsonify, request, current_app

feeding_blueprint = Blueprint('feeding', __name__)

@feeding_blueprint.route('/start', methods=['POST'])
def start_feeding():
    data = request.get_json() or {}
    plant_ip = data.get('plant_ip')
    plant_clients = current_app.config.get('plant_clients', {})
    if not plant_ip or plant_ip not in plant_clients:
        return jsonify({"status": "failure", "error": "Invalid or disconnected plant"}), 400

    client = plant_clients[plant_ip]
    try:
        client.emit('start_feeding', namespace='/status')
        return jsonify({"status": "success", "message": f"Feeding started for {plant_ip}"})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

@feeding_blueprint.route('/stop', methods=['POST'])
def stop_feeding():
    data = request.get_json() or {}
    plant_ip = data.get('plant_ip')
    plant_clients = current_app.config.get('plant_clients', {})
    if not plant_ip or plant_ip not in plant_clients:
        return jsonify({"status": "failure", "error": "Invalid or disconnected plant"}), 400

    client = plant_clients[plant_ip]
    try:
        client.emit('stop_feeding', namespace='/status')
        return jsonify({"status": "success", "message": f"Feeding stopped for {plant_ip}"})
    except Exception as e:
        return jsonify({"status": "failure", "error": str(e)}), 500

@feeding_blueprint.route('/status', methods=['GET'])
def get_feeding_status():
    return jsonify({"status": "not_implemented"})