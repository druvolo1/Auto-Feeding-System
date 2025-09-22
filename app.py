import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO
from flask_cors import CORS

# Blueprints (add more as we build)
from api.flow import flow_blueprint

# Services
from services.flow_service import get_latest_flow_rate, flow_reader

# Status namespace (for SocketIO broadcasts)
from status_namespace import StatusNamespace, set_socketio_instance

app = Flask(__name__)
CORS(app)

socketio = SocketIO(async_mode="eventlet", cors_allowed_origins="*")
socketio.init_app(app)
set_socketio_instance(socketio)
socketio.on_namespace(StatusNamespace('/status'))

# Register blueprints
app.register_blueprint(flow_blueprint, url_prefix='/api/flow')

# Background tasks
def broadcast_flow_rates():
    last_emitted_value = None
    while True:
        try:
            flow_rate = get_latest_flow_rate()
            if flow_rate is not None:
                flow_rate = round(flow_rate, 2)
                if flow_rate != last_emitted_value:
                    last_emitted_value = flow_rate
                    socketio.emit('flow_update', {'flow': flow_rate})
            eventlet.sleep(1)  # Update every second
        except Exception as e:
            print(f"Error broadcasting flow: {e}")

def start_threads():
    # Start flow reader (GPIO pulse counter)
    eventlet.spawn(flow_reader)
    
    # Start broadcaster
    eventlet.spawn(broadcast_flow_rates)

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == "__main__":
    start_threads()
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)