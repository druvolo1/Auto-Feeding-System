from flask import Blueprint, jsonify
from services.fresh_flow_service import get_latest_flow_rate as get_fresh_flow

fresh_flow_blueprint = Blueprint('fresh_flow', __name__)

@fresh_flow_blueprint.route('/latest', methods=['GET'])
def latest_flow():
    flow_value = get_fresh_flow()
    if flow_value is not None:
        return jsonify({'status': 'success', 'flow': flow_value}), 200
    return jsonify({'status': 'failure', 'message': 'No fresh flow reading available'}), 404