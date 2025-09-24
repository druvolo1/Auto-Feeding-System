from flask import Blueprint, jsonify, request, render_template
import json
import os

debug_blueprint = Blueprint('debug', __name__)

# Global debug states with hyphen keys
debug_states = {
    'fresh-flow': False,
    'feed-flow': False,
    'drain-flow': False,
    'socket-connections': False,
    'plants': False,
    'dns-resolution': False,  # Already added
    'local-websocket': False  # New debug flag for local WebSocket events
}

SETTINGS_FILE = os.path.join(os.getcwd(), "data", "settings.json")

def load_debug_states():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
            debug_settings = settings.get('debug_states', {})
            # Update debug_states with any keys from settings, defaulting to False if not in initial dict
            for key, value in debug_settings.items():
                if isinstance(value, bool):
                    debug_states[key] = value
                else:
                    debug_states[key] = False  # Default to False for unknown keys with non-boolean values

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
    if component and isinstance(enabled, bool):
        if component not in debug_states:
            debug_states[component] = False  # Add new component with default value
        debug_states[component] = enabled
        save_debug_states()  # Save on toggle
        return jsonify({"status": "success"})
    return jsonify({"status": "failure", "error": "Invalid component or value"}), 400