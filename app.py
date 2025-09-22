import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO
from flask_cors import CORS
import socketio as sio_module  # Renamed to avoid conflict
from threading import Lock, Event
import time
import socket

# Blueprints
from api.fresh_flow import fresh_flow_blueprint
from api.feed_flow import feed_flow_blueprint
from api.drain_flow import drain_flow_blueprint
from api.settings import settings_blueprint, load_settings
from api.debug import debug_blueprint, debug_states
from api.logs import log_blueprint
from api.valve_relay import valve_relay_blueprint
from api.feed_level import feed_level_blueprint

# Services
from services.fresh_flow_service import get_latest_flow_rate as get_latest_fresh_flow_rate, get_total_volume as get_fresh_total_volume, reset_total as reset_fresh_total, flow_reader as fresh_flow_reader
from services.feed_flow_service import get_latest_flow_rate as get_latest_feed_flow_rate, get_total_volume as get_feed_total_volume, reset_total as reset_feed_total, flow_reader as feed_flow_reader
from services.drain_flow_service import get_latest_flow_rate as get_latest_drain_flow_rate, get_total_volume as get_drain_total_volume, reset_total as reset_drain_total, flow_reader as drain_flow_reader
from services.valve_relay_service import reinitialize_relay_service, get_relay_status
from services.feed_level_service import get_feed_level

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
app.register_blueprint(settings_blueprint, url_prefix='/api/settings')
app.register_blueprint(debug_blueprint, url_prefix='/debug')
app.register_blueprint(log_blueprint, url_prefix='/api/logs')
app.register_blueprint(valve_relay_blueprint, url_prefix='/api/valve_relay')
app.register_blueprint(feed_level_blueprint, url_prefix='/api/feed_level')

# Shared state for remote plants
plant_data = {}  # { 'plant_ip': {...} }
plant_lock = Lock()
plant_clients = {}  # { 'plant_ip': sio_client }
reload_event = Event()

def connect_to_remote_plant(plant):
    if plant in plant_clients:
        return  # Already connected

    try:
        # Resolve name to IP to debug resolution
        ip = socket.gethostbyname(plant)
        if debug_states.get('socket-connections', False):
            print(f"[DEBUG] Resolved {plant} to IP: {ip}")
    except socket.gaierror as e:
        if debug_states.get('socket-connections', False):
            print(f"[ERROR] Name resolution failed for {plant}: {e}")
        return

    sio = sio_module.Client()
    plant_clients[plant] = sio

    @sio.event(namespace='/status')
    def connect():
        if debug_states.get('socket-connections', False):
            print(f"[INFO] Connected to remote plant: {plant}")

    @sio.event(namespace='/status')
    def disconnect():
        if debug_states.get('socket-connections', False):
            print(f"[INFO] Disconnected from remote plant: {plant}")
        with plant_lock:
            if plant in plant_data:
                plant_data[plant]['last_update'] = None  # Mark as offline

    @sio.on('status_update', namespace='/status')
    def handle_status_update(data):
        if debug_states.get('plants', False):
            print(f"[DEBUG] Received status_update from {plant}: {data}")
        with plant_lock:
            data['last_update'] = time.time() * 1000  # Milliseconds for JS
            data['ip'] = plant  # For identification
            data['system_name'] = data['settings'].get('system_name', plant)
            data['plant_name'] = data['settings'].get('plant_info', {}).get('name', 'N/A')
            data['start_date'] = data['settings'].get('plant_info', {}).get('start_date', 'N/A')
            plant_data[plant] = data

    try:
        sio.connect(f'http://{plant}:8000', namespaces=['/status'])
        if debug_states.get('socket-connections', False):
            print(f"[DEBUG] Connect attempt to {plant} initiated")
    except Exception as e:
        if debug_states.get('socket-connections', False):
            print(f"[ERROR] Failed to connect to {plant}: {e}")

def reload_plants():
    if debug_states.get('plants', False):
        print("[DEBUG] Reloading plants...")
    settings = load_settings()
    additional_plants = settings.get('additional_plants', [])
    if debug_states.get('plants', False):
        print(f"[DEBUG] Loaded additional_plants: {additional_plants}")
    
    # Connect to new plants
    for plant in additional_plants:
        if plant not in plant_clients:
            connect_to_remote_plant(plant)
    
    # Disconnect from removed plants
    for plant in list(plant_clients.keys()):
        if plant not in additional_plants:
            plant_clients[plant].disconnect()
            del plant_clients[plant]
            with plant_lock:
                if plant in plant_data:
                    del plant_data[plant]

def monitor_remote_plants():
    # Initial load on startup
    reload_plants()
    
    while True:
        reload_event.wait()  # Wait for signal
        if debug_states.get('plants', False):
            print("[DEBUG] Reload event triggered")
        reload_event.clear()
        reload_plants()

def broadcast_plants_status():
    last_emitted = None
    while True:
        try:
            with plant_lock:
                aggregated = {'plants': []}
                
                # Add remotes only
                for plant, data in plant_data.items():
                    aggregated['plants'].append(data)
                
                current_data = aggregated
                
                if current_data != last_emitted:
                    last_emitted = current_data
                    if debug_states.get('plants', False):
                        print(f"[DEBUG] Emitting plants_update: {len(current_data['plants'])} plants - Data: {current_data}")
                    socketio.emit('plants_update', current_data, namespace='/status')
            
            eventlet.sleep(5)  # Broadcast every 5 seconds
        except Exception as e:
            if debug_states.get('plants', False):
                print(f"[ERROR] Plants broadcast error: {e}")

# Background tasks
def broadcast_local_status():
    last_emitted = {
        'fresh_flow': None, 'fresh_total_volume': None,
        'feed_flow': None, 'feed_total_volume': None,
        'drain_flow': None, 'drain_total_volume': None,
        'relay1_status': None, 'relay2_status': None,
        'feed_level': None
    }
    while True:
        try:
            fresh_flow_rate = get_latest_fresh_flow_rate()
            fresh_total_volume = get_fresh_total_volume()
            feed_flow_rate = get_latest_feed_flow_rate()
            feed_total_volume = get_feed_total_volume()
            drain_flow_rate = get_latest_drain_flow_rate()
            drain_total_volume = get_drain_total_volume()
            relay1_status = get_relay_status(1)
            relay2_status = get_relay_status(2)
            feed_level = get_feed_level()

            data = {
                'fresh_flow': round(fresh_flow_rate, 2) if fresh_flow_rate is not None else None,
                'fresh_total_volume': round(fresh_total_volume, 2) if fresh_total_volume is not None else None,
                'feed_flow': round(feed_flow_rate, 2) if feed_flow_rate is not None else None,
                'feed_total_volume': round(feed_total_volume, 2) if feed_total_volume is not None else None,
                'drain_flow': round(drain_flow_rate, 2) if drain_flow_rate is not None else None,
                'drain_total_volume': round(drain_total_volume, 2) if drain_total_volume is not None else None,
                'relay1_status': relay1_status,
                'relay2_status': relay2_status,
                'feed_level': feed_level
            }

            if data != last_emitted:
                last_emitted = data
                if debug_states.get('socket-connections', False):
                    print(f"[DEBUG] Emitting local_status_update: {data}")
                socketio.emit('local_status_update', data, namespace='/status')
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
        eventlet.spawn(broadcast_local_status)
        print("[INIT] Starting plants monitor thread...")
        eventlet.spawn(monitor_remote_plants)
        print("[INIT] Starting plants status broadcast thread...")
        eventlet.spawn(broadcast_plants_status)
    except Exception as e:
        print(f"[ERROR] Failed to start threads: {e}")

# Call start_threads here (runs on module import for Gunicorn)
start_threads()

import services.log_service

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/settings')
def settings():
    return render_template('settings.html')

@app.route('/debug')
def debug_page():
    return render_template('debug.html')

@app.route('/logs')
def logs_page():
    return render_template('logs.html')

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)