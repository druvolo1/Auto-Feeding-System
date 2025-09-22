from flask import Blueprint, jsonify, request, render_template
import json
import os

debug_blueprint = Blueprint('debug', __name__)

# Global debug states
debug_states = {
    'fresh_flow': False,
    'feed_flow': False,
    'drain_flow': False,
    'socket_connections': False,
    'plants': False
}

SETTINGS_FILE = os.path.join(os.getcwd(), "data", "settings.json")

def load_debug_states():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
            debug_settings = settings.get('debug_states', {})
            for key in debug_states:
                if key in debug_settings:
                    debug_states[key] = debug_settings[key]

def save_debug_states():
    settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
    settings['debug_states'] = debug_states
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=4)

# Load on startup
load_debug_states()

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
        save_debug_states()  # Save on toggle
        return jsonify({"status": "success"})
    return jsonify({"status": "failure", "error": "Invalid component or value"}), 400