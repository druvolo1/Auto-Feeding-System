from flask import Blueprint, jsonify
from services.feed_flow_service import get_total_volume, reset_total

feed_flow_blueprint = Blueprint('feed_flow', __name__)

@feed_flow_blueprint.route('/reset', methods=['POST'])
def reset():
    from services.log_service import log_reset_event
    previous_total = get_total_volume()
    reset_total()
    log_reset_event('feed_flow', previous_total)
    return jsonify({"status": "success"})