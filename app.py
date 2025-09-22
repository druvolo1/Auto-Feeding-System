import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO
from flask_cors import CORS

# Blueprints
from api.fresh_flow import fresh_flow_blueprint

# Services
from services.fresh_flow_service import get_latest_flow_rate, get_total_volume, reset_total, flow_reader

# Status namespace
from status_namespace import StatusNamespace, set_socketio_instance

app = Flask(__name__)
CORS(app)

socketio = SocketIO(async_mode="eventlet", cors_allowed_origins="*")
socketio.init_app(app)
set_socketio_instance(socketio)
socketio.on_namespace(StatusNamespace('/status'))

# Register blueprints
app.register_blueprint(fresh_flow_blueprint, url_prefix='/api/fresh_flow')

# Background tasks
def broadcast_flow_rates():
    last_emitted = {'flow': None, 'total_volume': None}
    while True:
        try:
            flow_rate = get_latest_flow_rate()
            total_volume = get_total_volume()

            data = {
                'flow': round(flow_rate, 2) if flow_rate is not None else None,
                'total_volume': round(total_volume, 2) if total_volume is not None else None
            }

            if data != last_emitted:
                last_emitted = data
                print(f"[DEBUG] Emitting flow_update: {data}")
                socketio.emit('flow_update', data, namespace='/status')
            eventlet.sleep(1)
        except Exception as e:
            print(f"[ERROR] Broadcast error: {e}")

def start_threads():
    try:
        print("[INIT] Starting fresh flow reader thread...")
        eventlet.spawn(flow_reader)
        print("[INIT] Starting broadcast thread...")
        eventlet.spawn(broadcast_flow_rates)
    except Exception as e:
        print(f"[ERROR] Failed to start threads: {e}")

# Call start_threads here (runs on module import for Gunicorn)
start_threads()

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)