import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO
from flask_cors import CORS
import socketio as sio_module
from threading import Lock, Event
import time
import socket
from datetime import datetime

# Import load_settings directly
from api.settings import load_settings

# Blueprints (imported later to avoid circular import)
fresh_flow_blueprint = None
feed_flow_blueprint = None
drain_flow_blueprint = None
settings_blueprint = None
debug_blueprint = None
log_blueprint = None
valve_relay_blueprint = None
feed_level_blueprint = None
feed_pump_blueprint = None
feeding_blueprint = None

# Services
from services.fresh_flow_service import get_latest_flow_rate as get_latest_fresh_flow_rate, get_total_volume as get_fresh_total_volume, reset_total as reset_fresh_total, flow_reader as fresh_flow_reader
from services.feed_flow_service import get_latest_flow_rate as get_latest_feed_flow_rate, get_total_volume as get_feed_total_volume, reset_total as reset_feed_total, flow_reader as feed_flow_reader
from services.drain_flow_service import get_latest_flow_rate as get_latest_drain_flow_rate, get_total_volume as get_drain_total_volume, reset_total as reset_drain_total, flow_reader as drain_flow_reader
from services.valve_relay_service import reinitialize_relay_service, get_relay_status
from services.feed_level_service import get_feed_level
from services.log_service import log_event

# Status namespace
from status_namespace import StatusNamespace, set_socketio_instance

# mDNS helper
from utils.mdns_utils import standardize_host_ip

app = Flask(__name__)
CORS(app)

# Load settings into app.config for access via current_app.config['settings']
app.config['settings'] = load_settings()
app.config['plant_data'] = {}
app.config['plant_lock'] = Lock()
app.config['plant_clients'] = {}
app.config['reload_event'] = Event()

socketio = SocketIO(async_mode="eventlet", cors_allowed_origins="*")
socketio.init_app(app)
set_socketio_instance(socketio)
socketio.on_namespace(StatusNamespace('/status'))

# Register blueprints after app initialization
def register_blueprints():
    global fresh_flow_blueprint, feed_flow_blueprint, drain_flow_blueprint, settings_blueprint, debug_blueprint, log_blueprint, valve_relay_blueprint, feed_level_blueprint, feed_pump_blueprint, feeding_blueprint
    from api.fresh_flow import fresh_flow_blueprint
    from api.feed_flow import feed_flow_blueprint
    from api.drain_flow import drain_flow_blueprint
    from api.settings import settings_blueprint
    from api.debug import debug_blueprint
    from api.logs import log_blueprint
    from api.valve_relay import valve_relay_blueprint
    from api.feed_level import feed_level_blueprint
    from api.feed_pump import feed_pump_blueprint
    from api.feeding import feeding_blueprint

    app.register_blueprint(fresh_flow_blueprint, url_prefix='/api/fresh_flow')
    app.register_blueprint(feed_flow_blueprint, url_prefix='/api/feed_flow')
    app.register_blueprint(drain_flow_blueprint, url_prefix='/api/drain_flow')
    app.register_blueprint(settings_blueprint, url_prefix='/api/settings')
    app.register_blueprint(debug_blueprint, url_prefix='/debug')
    app.register_blueprint(log_blueprint, url_prefix='/api/logs')
    app.register_blueprint(valve_relay_blueprint, url_prefix='/api/valve_relay')
    app.register_blueprint(feed_level_blueprint, url_prefix='/api/feed_level')
    app.register_blueprint(feed_pump_blueprint, url_prefix='/api/feed_pump')
    app.register_blueprint(feeding_blueprint, url_prefix='/api/feeding')

# Call register_blueprints after app setup
register_blueprints()

# Pass app instance to feeding_service
from services.feeding_service import initialize_feeding_service
initialize_feeding_service(app, socketio)

# Shared state for remote plants
plant_data = app.config['plant_data']
plant_lock = app.config['plant_lock']
plant_clients = app.config['plant_clients']
reload_event = app.config['reload_event']

def log_feeding_feedback(message, plant_ip=None, status='info'):
    """
    Log feeding feedback to both the UI (via SocketIO) and feeding.jsonl.
    """
    log_data = {
        'event_type': 'feeding_feedback',
        'message': message,
        'status': status,
        'timestamp': datetime.now().isoformat()
    }
    if plant_ip:
        log_data['plant_ip'] = plant_ip
    
    socketio.emit('feeding_feedback', log_data, namespace='/status')
    log_event(log_data, category='feeding')

def connect_to_remote_plant(plant):
    if plant in plant_clients:
        if debug_states.get('socket-connections', False):
            print(f"[DEBUG] Plant {plant} already connected")
        log_feeding_feedback(f"Plant {plant} already connected", plant, status='info')
        return

    ip = standardize_host_ip(plant)
    if not ip:
        log_feeding_feedback(f"Name resolution failed for {plant}, resolved IP: {ip}", plant, status='error')
        if debug_states.get('socket-connections', False):
            print(f"[ERROR] Name resolution failed for {plant}, resolved IP: {ip}")
        return

    if debug_states.get('socket-connections', False):
        print(f"[DEBUG] Resolved {plant} to IP: {ip} for connection")
    log_feeding_feedback(f"Resolved {plant} to IP: {ip} for connection", plant, status='info')

    sio = sio_module.Client()
    plant_clients[plant] = sio

    @sio.event(namespace='/status')
    def connect():
        if debug_states.get('socket-connections', False):
            print(f"[INFO] Connected to remote plant: {plant} at {ip}")
        log_feeding_feedback(f"Connected to remote plant: {plant} at {ip}", plant, status='success')
        with plant_lock:
            if plant in plant_data:
                plant_data[plant]['is_online'] = True

    @sio.event(namespace='/status')
    def disconnect():
        if debug_states.get('socket-connections', False):
            print(f"[INFO] Disconnected from remote plant: {plant} at {ip}")
        log_feeding_feedback(f"Disconnected from remote plant: {plant} at {ip}", plant, status='info')
        with plant_lock:
            if plant in plant_data:
                plant_data[plant]['last_update'] = None
                plant_data[plant]['is_online'] = False

    @sio.on('status_update', namespace='/status')
    def handle_status_update(data):
        if debug_states.get('plants', False):
            print(f"[DEBUG] Received status_update from {plant} at {ip}: {data}")
        if debug_states.get('feeding', False):
            print(f"[DEBUG] Feeding status from {plant}: in_progress={data.get('feeding_in_progress')}, allowed={data['settings'].get('allow_remote_feeding')}")
        with plant_lock:
            data['last_update'] = time.time() * 1000
            data['ip'] = plant
            data['system_name'] = data['settings'].get('system_name', plant)
            data['plant_name'] = data['settings'].get('plant_info', {}).get('name', 'N/A')
            data['start_date'] = data['settings'].get('plant_info', {}).get('start_date', 'N/A')
            data['is_online'] = True
            plant_data[plant] = data

    try:
        sio.connect(f'http://{ip}:8000', namespaces=['/status'])
        if debug_states.get('socket-connections', False):
            print(f"[DEBUG] Connect attempt to {plant} at {ip}:8000 succeeded")
        log_feeding_feedback(f"Connection succeeded to {plant} at {ip}:8000", plant, status='success')
    except Exception as e:
        log_feeding_feedback(f"Failed to connect to {plant} at {ip}:8000: {str(e)}", plant, status='error')
        if debug_states.get('socket-connections', False):
            print(f"[ERROR] Failed to connect to {plant} at {ip}:8000: {str(e)}")

def reload_plants():
    if debug_states.get('plants', False):
        print("[DEBUG] Reloading plants...")
    settings = load_settings()
    additional_plants = settings.get('additional_plants', [])
    if debug_states.get('plants', False):
        print(f"[DEBUG] Loaded additional_plants: {additional_plants}")
    log_feeding_feedback(f"Loaded {len(additional_plants)} additional plants: {additional_plants}", status='info')
    
    if not additional_plants:
        log_feeding_feedback("No additional plants configured in settings", status='error')
    
    normalized_plants = {}
    for plant in additional_plants:
        ip = standardize_host_ip(plant)
        if ip:
            normalized_plants[ip] = plant
    
    for ip, plant in normalized_plants.items():
        if ip not in [standardize_host_ip(p) for p in plant_clients.keys()]:
            connect_to_remote_plant(plant)
    
    for plant in list(plant_clients.keys()):
        if standardize_host_ip(plant) not in [standardize_host_ip(p) for p in normalized_plants.values()]:
            plant_clients[plant].disconnect()
            log_feeding_feedback(f"Disconnected removed plant {plant}", plant, status='info')
            del plant_clients[plant]
            with plant_lock:
                if plant in plant_data:
                    del plant_data[plant]
    
    # Log the state of plant_clients
    connected_plants = [plant for plant, client in plant_clients.items() if client.connected]
    log_feeding_feedback(f"Plant clients after reload: {connected_plants}", status='info')

def monitor_remote_plants():
    # Initial load on startup
    reload_plants()
    
    while True:
        reload_event.wait()
        if debug_states.get('plants', False):
            print("[DEBUG] Reload event triggered")
        log_feeding_feedback("Reload event triggered for plants", status='info')
        reload_event.clear()
        reload_plants()

def broadcast_plants_status():
    while True:
        try:
            settings = load_settings()
            additional_plants = settings.get('additional_plants', [])
            
            with plant_lock:
                aggregated = {'plants': []}
                
                for plant_ip in additional_plants:
                    resolved_ip = standardize_host_ip(plant_ip)
                    plant_data_entry = plant_data.get(plant_ip, {})
                    is_online = plant_data_entry.get('is_online', False) if plant_ip in plant_clients else False
                    if plant_ip in plant_data and plant_data[plant_ip].get('last_update'):
                        plant_data[plant_ip]['ip'] = resolved_ip or plant_ip
                        aggregated['plants'].append(plant_data[plant_ip])
                    else:
                        aggregated['plants'].append({
                            'ip': resolved_ip or plant_ip,
                            'system_name': plant_ip,
                            'plant_name': 'Offline',
                            'start_date': 'N/A',
                            'settings': {
                                'system_volume': 'N/A',
                                'allow_remote_feeding': False,
                                'plant_info': {}
                            },
                            'current_ph': None,
                            'feeding_in_progress': False,
                            'last_update': None,
                            'water_level': {},
                            'valve_info': {
                                'fill_valve_label': '',
                                'drain_valve_label': '',
                                'valve_relays': {},
                                'fill_valve_ip': '',
                                'fill_valve': '',
                                'drain_valve_ip': '',
                                'drain_valve': ''
                            },
                            'is_online': is_online
                        })
                
                current_data = aggregated
                
                if debug_states.get('plants', False):
                    print(f"[DEBUG] Emitting plants_update: {len(current_data['plants'])} plants - Data: {current_data}")
                socketio.emit('plants_update', current_data, namespace='/status')
            
            eventlet.sleep(5)
        except Exception as e:
            if debug_states.get('plants', False):
                print(f"[ERROR] Plants broadcast error: {e}")
            log_feeding_feedback(f"Plants broadcast error: {str(e)}", status='error')
            eventlet.sleep(5)

def broadcast_local_status():
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
            if debug_states.get('local-websocket', False) or debug_states.get('socket-connections', False):
                print(f"[DEBUG] Emitting local_status_update: {data}")

            socketio.emit('local_status_update', data, namespace='/status')
            eventlet.sleep(1)
        except Exception as e:
            print(f"[ERROR] Broadcast error: {e}")
            log_feeding_feedback(f"Local status broadcast error: {str(e)}", status='error')

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
        log_feeding_feedback(f"Failed to start threads: {str(e)}", status='error')

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
    # Resolve host for socketio.run to handle mDNS
    host = standardize_host_ip("0.0.0.0") or "0.0.0.0"
    socketio.run(app, host=host, port=8000, debug=True)