from flask import Blueprint, jsonify
from services.drain_flow_service import get_total_volume, reset_total

drain_flow_blueprint = Blueprint('drain_flow', __name__)

@drain_flow_blueprint.route('/reset', methods=['POST'])
def reset():
    from services.log_service import log_reset_event
    previous_total = get_total_volume()
    reset_total()
    log_reset_event('drain_flow', previous_total)
    return jsonify({"status": "success"})