from flask import Blueprint, jsonify
from services.feed_flow_service import get_latest_flow_rate, reset_total

feed_flow_blueprint = Blueprint('feed_flow', __name__)

@feed_flow_blueprint.route('/latest', methods=['GET'])
def latest_flow():
    flow_value = get_latest_flow_rate()
    if flow_value is not None:
        return jsonify({'status': 'success', 'flow': flow_value}), 200
    return jsonify({'status': 'failure', 'message': 'No feed flow reading available'}), 404

@feed_flow_blueprint.route('/reset', methods=['POST'])
def reset_flow():
    try:
        reset_total()
        return jsonify({'status': 'success', 'message': 'Total volume reset'}), 200
    except Exception as e:
        return jsonify({'status': 'failure', 'message': f'Failed to reset total volume: {str(e)}'}), 500