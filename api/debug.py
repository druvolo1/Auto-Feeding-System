from flask import Blueprint, jsonify, request, render_template

debug_blueprint = Blueprint('debug', __name__)

# Global debug states
debug_states = {
    'fresh_flow': False,
    'feed_flow': False,
    'drain_flow': False,
    'socket_connections': False,
    'plants': False
}

@debug_blueprint.route('/states', methods=['GET'])
def get_debug_states():
    return jsonify(debug_states)

@debug_blueprint.route('/toggle', methods=['POST'])
def toggle_debug():
    data = request.get_json() or {}
    component = data.get('component')
    enabled = data.get('enabled')
    if component in debug_states and isinstance(enabled, bool):
        debug_states[component] = enabled
        return jsonify({"status": "success"})
    return jsonify({"status": "failure", "error": "Invalid component or value"}), 400