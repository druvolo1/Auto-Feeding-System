from flask import Blueprint, jsonify
from services.flow_service import get_latest_flow_rate

flow_blueprint = Blueprint('flow', __name__)

@flow_blueprint.route('/latest', methods=['GET'])
def latest_flow():
    flow_value = get_latest_flow_rate()
    if flow_value is not None:
        return jsonify({'status': 'success', 'flow': flow_value}), 200
    return jsonify({'status': 'failure', 'message': 'No flow reading available'}), 404