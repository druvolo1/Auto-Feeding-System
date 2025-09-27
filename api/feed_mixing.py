from flask import Blueprint, jsonify

feed_mixing_blueprint = Blueprint('feed_mixing', __name__)

@feed_mixing_blueprint.route('/status', methods=['GET'])
def get_mixing_status():
    """
    Get the current mixing status (placeholder; can be expanded to check active mixing).
    """
    from flask import current_app
    phase = current_app.config.get('current_feeding_phase', 'idle')
    return jsonify({"status": phase, "message": "Mixing monitor is running" if phase == 'fill' else "Idle"})