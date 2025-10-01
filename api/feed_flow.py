from flask import Blueprint, jsonify
from services.feed_flow_service import get_total_volume, reset_total

feed_flow_blueprint = Blueprint('feed_flow', __name__)

calibration_factor = 28.390575  # Default calibration factor (pulses per gallon)

def set_calibration_factor(new_factor):
    global calibration_factor
    if not isinstance(new_factor, (int, float)) or new_factor <= 0:
        raise ValueError("Calibration factor must be a positive number")
    calibration_factor = float(new_factor)
    # TODO: If needed, propagate to service layer or hardware

@feed_flow_blueprint.route('/reset', methods=['POST'])
def reset():
    from services.log_service import log_reset_event
    previous_total = get_total_volume()
    reset_total()
    log_reset_event('feed_flow', previous_total)
    return jsonify({"status": "success"})