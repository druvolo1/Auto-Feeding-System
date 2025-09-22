import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO
from flask_cors import CORS

# Blueprints
from api.fresh_flow import fresh_flow_blueprint
from api.feed_flow import feed_flow_blueprint
from api.drain_flow import drain_flow_blueprint

# Services
from services.fresh_flow_service import get_latest_flow_rate as get_latest_fresh_flow_rate, get_total_volume as get_fresh_total_volume, reset_total as reset_fresh_total, flow_reader as fresh_flow_reader
from services.feed_flow_service import get_latest_flow_rate as get_latest_feed_flow_rate, get_total_volume as get_feed_total_volume, reset_total as reset_feed_total, flow_reader as feed_flow_reader
from services.drain_flow_service import get_latest_flow_rate as get_latest_drain_flow_rate, get_total_volume as get_drain_total_volume, reset_total as reset_drain_total, flow_reader as drain_flow_reader

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
app.register_blueprint(feed_flow_blueprint, url_prefix='/api/feed_flow')
app.register_blueprint(drain_flow_blueprint, url_prefix='/api/drain_flow')

# Background tasks
def broadcast_flow_rates():
    last_emitted = {
        'fresh_flow': None, 'fresh_total_volume': None,
        'feed_flow': None, 'feed_total_volume': None,
        'drain_flow': None, 'drain_total_volume': None
    }
    while True:
        try:
            fresh_flow_rate = get_latest_fresh_flow_rate()
            fresh_total_volume = get_fresh_total_volume()
            feed_flow_rate = get_latest_feed_flow_rate()
            feed_total_volume = get_feed_total_volume()
            drain_flow_rate = get_latest_drain_flow_rate()
            drain_total_volume = get_drain_total_volume()

            data = {
                'fresh_flow': round(fresh_flow_rate, 2) if fresh_flow_rate is not None else None,
                'fresh_total_volume': round(fresh_total_volume, 2) if fresh_total_volume is not None else None,
                'feed_flow': round(feed_flow_rate, 2) if feed_flow_rate is not None else None,
                'feed_total_volume': round(feed_total_volume, 2) if feed_total_volume is not None else None,
                'drain_flow': round(drain_flow_rate, 2) if drain_flow_rate is not None else None,
                'drain_total_volume': round(drain_total_volume, 2) if drain_total_volume is not None else None
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
        eventlet.spawn(fresh_flow_reader)
        print("[INIT] Starting feed flow reader thread...")
        eventlet.spawn(feed_flow_reader)
        print("[INIT] Starting drain flow reader thread...")
        eventlet.spawn(drain_flow_reader)
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