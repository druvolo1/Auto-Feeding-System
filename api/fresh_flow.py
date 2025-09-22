from flask import Blueprint, jsonify
from services.fresh_flow_service import get_latest_flow_rate, reset_total

fresh_flow_blueprint = Blueprint('fresh_flow', __name__)

@fresh_flow_blueprint.route('/latest', methods=['GET'])
def latest_flow():
    flow_value = get_latest_flow_rate()
    if flow_value is not None:
        return jsonify({'status': 'success', 'flow': flow_value}), 200
    return jsonify({'status': 'failure', 'message': 'No fresh flow reading available'}), 404

@fresh_flow_blueprint.route('/reset', methods=['POST'])
def reset_flow():
    try:
        reset_total()
        return jsonify({'status': 'success', 'message': 'Total volume reset'}), 200
    except Exception as e:
        return jsonify({'status': 'failure', 'message': f'Failed to reset total volume: {str(e)}'}), 500