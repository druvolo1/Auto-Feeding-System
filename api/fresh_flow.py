from flask import Blueprint, jsonify
from services.fresh_flow_service import get_total_volume, reset_total

fresh_flow_blueprint = Blueprint('fresh_flow', __name__)

@fresh_flow_blueprint.route('/reset', methods=['POST'])
def reset():
    from services.log_service import log_reset_event
    previous_total = get_total_volume()
    reset_total()
    log_reset_event('fresh_flow', previous_total)
    return jsonify({"status": "success"})