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
import requests

# Define debug_states globally
debug_states = {
    'plants': False,
    'socket-connections': False,
    'feeding': False,
    'local-websocket': False,
    'notifications': False
}

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
feed_mixing_blueprint = None
plants_blueprint = None

# Services
from services.fresh_flow_service import get_latest_flow_rate as get_latest_fresh_flow_rate, get_total_volume as get_fresh_total_volume, reset_total as reset_fresh_total, flow_reader as fresh_flow_reader
from services.feed_flow_service import get_latest_flow_rate as get_latest_feed_flow_rate, get_total_volume as get_feed_total_volume, reset_total as reset_feed_total, flow_reader as feed_flow_reader
from services.drain_flow_service import get_latest_flow_rate as get_latest_drain_flow_rate, get_total_volume as get_drain_total_volume, reset_total as reset_drain_total, flow_reader as drain_flow_reader
from services.valve_relay_service import reinitialize_relay_service, get_relay_status
from services.feed_level_service import get_feed_level
from services.log_service import log_event, prune_logs_daily
from services.feed_mixing_service import monitor_feed_mixing

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
app.config['debug_states'] = debug_states
app.config['feeding_sequence_active'] = False
debug_states.update(app.config['settings'].get('debug_states', {}))

socketio = SocketIO(async_mode="eventlet", cors_allowed_origins="*")
socketio.init_app(app)
set_socketio_instance(socketio)
socketio.on_namespace(StatusNamespace('/status'))

# Register blueprints after app initialization
def register_blueprints():
    global fresh_flow_blueprint, feed_flow_blueprint, drain_flow_blueprint, settings_blueprint, debug_blueprint, log_blueprint, valve_relay_blueprint, feed_level_blueprint, feed_pump_blueprint, feeding_blueprint, feed_mixing_blueprint, plants_blueprint
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
    from api.feed_mixing import feed_mixing_blueprint
    from api.plants import plants_blueprint

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
    app.register_blueprint(feed_mixing_blueprint, url_prefix='/api/feed_mixing')
    app.register_blueprint(plants_blueprint, url_prefix='/api/plants')

# Call register_blueprints after app setup
register_blueprints()

# Load and set calibration factors from settings.json after blueprints are registered
calibration_factors = app.config['settings'].get('calibration_factors', {})
from api.fresh_flow import set_calibration_factor as set_fresh_cf
from api.feed_flow import set_calibration_factor as set_feed_cf
from api.drain_flow import set_calibration_factor as set_drain_cf
set_fresh_cf(calibration_factors.get('fresh', 28.390575))
set_feed_cf(calibration_factors.get('feed', 28.390575))
set_drain_cf(calibration_factors.get('drain', 28.390575))

# Pass app instance to feeding_service
from services.feeding_service import initialize_feeding_service
initialize_feeding_service(app, socketio)

# Shared state for remote plants
plant_data = app.config['plant_data']
plant_lock = app.config['plant_lock']
plant_clients = app.config['plant_clients']
reload_event = app.config['reload_event']

# Remote plant liveness tuning.
# Zones de-duplicate their status emits, so AFS drives its own heartbeat via
# request_refresh; last_update age is the only liveness signal we trust.
PLANT_HEARTBEAT_INTERVAL = 10   # seconds between request_refresh per plant
PLANT_STALE_AFTER = 35          # 3 missed heartbeats -> data is not trustworthy
PLANT_DEAD_AFTER = 70           # -> eligible for one rebuild
PLANT_WATCHDOG_INTERVAL = 15    # seconds between watchdog sweeps

# Recovery is deliberately bounded and single-owner:
#  - the socket.io client's own auto-reconnect is DISABLED (see connect_to_remote_plant),
#    so only the watchdog ever rebuilds a connection and the two can never fight,
#  - one rebuild attempt at a time, spaced by this escalating schedule,
#  - the delay never drops below the last step, so a zone that stays down settles at
#    one quiet attempt every 10 minutes instead of churning,
#  - no rebuilds at all while a feeding sequence is running.
PLANT_RETRY_SCHEDULE = [15, 30, 60, 120, 300, 600]  # seconds; last value repeats
PLANT_ALERT_AFTER_FAILURES = 5  # notify once at this many consecutive failures
PLANT_STABLE_AFTER = 120        # a rebuilt link must hold this long before the
                                # backoff resets, so a flapping zone keeps backing
                                # off instead of being rebuilt every sweep
app.config['PLANT_STALE_AFTER'] = PLANT_STALE_AFTER

# Per-host recovery bookkeeping, exposed via /api/plants/diagnostics so the
# behaviour is observable rather than mysterious.
plant_recovery = {}
app.config['plant_recovery'] = plant_recovery


# Hosts with a connect attempt in flight. Prevents the startup reload and the
# watchdog (or two watchdog sweeps) from opening duplicate sockets to one zone,
# which would orphan a client that nothing can ever clean up.
_connect_in_progress = set()


def _recovery_record(plant):
    # recovery_attempts counts attempts in the CURRENT outage episode and drives the
    # backoff. It is cleared only by a link that stays healthy for PLANT_STABLE_AFTER,
    # so a zone that connects and immediately drops keeps backing off rather than
    # being rebuilt every sweep.
    return plant_recovery.setdefault(plant, {
        'recovery_attempts': 0,
        'total_rebuilds': 0,
        'last_attempt': None,
        'last_success': None,
        'next_attempt': 0,
        'alerted': False,
        'last_error': None,
        'healthy_since': None,
    })

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

def send_notification(alert_text: str):
    """
    Send notification to Discord and/or Telegram if enabled.
    Prepends the system name to the alert.
    """
    settings = load_settings()
    system_name = settings.get("system_name", "FlowMeter")
    final_alert = f"[{system_name}] {alert_text}"

    if debug_states.get('notifications', False):
        print(f"[DEBUG] Sending notification: {final_alert}")

    # --- Discord ---
    if settings.get("discord_enabled"):
        webhook_url = settings.get("discord_webhook_url", "").strip()
        if webhook_url:
            try:
                resp = requests.post(webhook_url, json={"content": final_alert}, timeout=10)
                if debug_states.get('notifications', False):
                    print(f"[DEBUG] Discord POST => {resp.status_code}")
            except Exception as ex:
                if debug_states.get('notifications', False):
                    print(f"[ERROR] Discord send failed: {ex}")
        else:
            if debug_states.get('notifications', False):
                print("[DEBUG] Discord enabled but missing webhook_url, skipping...")

    # --- Telegram ---
    if settings.get("telegram_enabled"):
        bot_token = settings.get("telegram_bot_token", "").strip()
        chat_id = settings.get("telegram_chat_id", "").strip()
        if bot_token and chat_id:
            try:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {"chat_id": chat_id, "text": final_alert}
                resp = requests.post(url, json=payload, timeout=10)
                if debug_states.get('notifications', False):
                    print(f"[DEBUG] Telegram POST => {resp.status_code}")
            except Exception as ex:
                if debug_states.get('notifications', False):
                    print(f"[ERROR] Telegram send failed: {ex}")
        else:
            if debug_states.get('notifications', False):
                print("[DEBUG] Telegram enabled but missing bot_token/chat_id, skipping...")

def plant_age_seconds(plant):
    """Seconds since the last status_update from this plant, or None if never seen."""
    last_update = plant_data.get(plant, {}).get('last_update')
    if not last_update:
        return None
    return max(0.0, time.time() - (last_update / 1000.0))


def connect_to_remote_plant(plant):
    """Build a socket.io client for `plant`. Returns True only on a live connection.

    The client is registered in plant_clients ONLY after connect() succeeds, so a
    failed attempt never leaves a permanently dead entry behind (which used to make
    reload_plants skip the host forever).
    """
    existing = plant_clients.get(plant)
    if existing is not None and existing.connected:
        if debug_states.get('socket-connections', False):
            print(f"[DEBUG] Plant {plant} already connected")
        return True

    if plant in _connect_in_progress:
        if debug_states.get('socket-connections', False):
            print(f"[DEBUG] Connect already in flight for {plant}, skipping")
        return False
    _connect_in_progress.add(plant)
    try:
        return _do_connect(plant)
    finally:
        _connect_in_progress.discard(plant)


def _do_connect(plant):
    ip = standardize_host_ip(plant)
    if not ip:
        log_feeding_feedback(f"Name resolution failed for {plant}, resolved IP: {ip}", plant, status='error')
        if debug_states.get('socket-connections', False):
            print(f"[ERROR] Name resolution failed for {plant}, resolved IP: {ip}")
        return False

    if debug_states.get('socket-connections', False):
        print(f"[DEBUG] Resolved {plant} to IP: {ip} for connection")

    # Auto-reconnect is OFF on purpose. The watchdog is the single owner of
    # recovery; with both enabled a dead zone gets two independent retry loops.
    sio = sio_module.Client(reconnection=False, request_timeout=10)

    @sio.event(namespace='/status')
    def connect():
        try:
            if debug_states.get('socket-connections', False):
                print(f"[INFO] Connected to remote plant: {plant} at {ip}")
            log_feeding_feedback(f"Connected to remote plant: {plant} at {ip}", plant, status='success')
            with plant_lock:
                if plant in plant_data:
                    plant_data[plant]['is_online'] = True
        except Exception as e:
            print(f"[ERROR] connect handler failed for {plant}: {e}")

    @sio.event(namespace='/status')
    def disconnect():
        try:
            if debug_states.get('socket-connections', False):
                print(f"[INFO] Disconnected from remote plant: {plant} at {ip}")
            log_feeding_feedback(f"Disconnected from remote plant: {plant} at {ip}", plant, status='info')
            with plant_lock:
                if plant in plant_data:
                    plant_data[plant]['last_update'] = None
                    plant_data[plant]['is_online'] = False
        except Exception as e:
            print(f"[ERROR] disconnect handler failed for {plant}: {e}")

    @sio.on('status_update', namespace='/status')
    def handle_status_update(data):
        try:
            if not isinstance(data, dict) or 'settings' not in data:
                # Granular updates (valve_update etc.) are not full status payloads.
                if debug_states.get('plants', False):
                    print(f"[DEBUG] Ignoring malformed status_update from {plant}: {data}")
                return
            if debug_states.get('plants', False):
                print(f"[DEBUG] Received status_update from {plant} at {ip}: {data}")
            if debug_states.get('feeding', False):
                print(f"[DEBUG] Feeding status from {plant}: in_progress={data.get('feeding_in_progress')}, "
                      f"allowed={data.get('settings', {}).get('allow_remote_feeding')}")
            with plant_lock:
                data['last_update'] = time.time() * 1000
                data['ip'] = plant
                data['system_name'] = data['settings'].get('system_name', plant)
                data['plant_name'] = data['settings'].get('plant_info', {}).get('name', 'N/A')
                data['start_date'] = data['settings'].get('plant_info', {}).get('start_date', 'N/A')
                data['is_online'] = True
                plant_data[plant] = data
        except Exception as e:
            print(f"[ERROR] status_update handler failed for {plant}: {e}")

    try:
        sio.connect(f'http://{ip}:8000', namespaces=['/status'],
                    transports=['websocket'], wait=True, wait_timeout=10)
    except Exception as e:
        log_feeding_feedback(f"Failed to connect to {plant} at {ip}:8000: {str(e)}", plant, status='error')
        if debug_states.get('socket-connections', False):
            print(f"[ERROR] Failed to connect to {plant} at {ip}:8000: {str(e)}")
        eventlet.spawn(_abandon_client, sio)
        return False

    plant_clients[plant] = sio
    if debug_states.get('socket-connections', False):
        print(f"[DEBUG] Connect attempt to {plant} at {ip}:8000 succeeded")
    log_feeding_feedback(f"Connection succeeded to {plant} at {ip}:8000", plant, status='success')
    return True


def _abandon_client(client):
    """Time-boxed disconnect. A wedged client's disconnect() joins its background
    tasks and can block forever, so this must always run in its own greenlet."""
    try:
        with eventlet.Timeout(5, False):
            client.disconnect()
    except Exception:
        pass


def retire_client(plant):
    """Drop a plant's client and cached data. Unregisters first so nothing can
    observe a half-torn-down client, then abandons the socket asynchronously."""
    client = plant_clients.pop(plant, None)
    with plant_lock:
        plant_data.pop(plant, None)
    if client is not None:
        log_feeding_feedback(f"Retiring socket client for {plant}", plant, status='info')
        eventlet.spawn(_abandon_client, client)


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

    # Key off the configured host strings, not resolved IPs: a host that cannot be
    # resolved right now must stay in the retry set instead of silently vanishing.
    desired = list(dict.fromkeys(additional_plants))

    for plant in desired:
        if plant not in plant_clients:
            connect_to_remote_plant(plant)  # failure is fine, the watchdog retries

    for plant in list(plant_clients.keys()):
        if plant not in desired:
            retire_client(plant)
            log_feeding_feedback(f"Disconnected removed plant {plant}", plant, status='info')

    connected_plants = [p for p, c in list(plant_clients.items()) if c.connected]
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


def plant_heartbeat_loop():
    """Zone status emits are de-duplicated, so silence carries no information.
    Ask every connected zone for a forced refresh on a fixed cadence; that is what
    makes last_update a real heartbeat and staleness detectable."""
    while True:
        for plant, client in list(plant_clients.items()):
            try:
                if client.connected:
                    client.emit('request_refresh', namespace='/status')
            except Exception as e:
                if debug_states.get('socket-connections', False):
                    print(f"[DEBUG] Heartbeat emit failed for {plant}: {e}")
        eventlet.sleep(PLANT_HEARTBEAT_INTERVAL)


def plant_watchdog():
    """Rebuild clients that are missing, disconnected, or no longer producing data.

    Bounded by design: at most one rebuild per host per scheduled interval, the
    interval escalates to a 10-minute floor and never speeds back up until the host
    actually recovers, and nothing is rebuilt while a feeding sequence is running.
    """
    while True:
        # Sleep first: at startup reload_plants() is still opening connections, and
        # sweeping before it finishes would race it into duplicate sockets.
        eventlet.sleep(PLANT_WATCHDOG_INTERVAL)
        try:
            if app.config.get('feeding_sequence_active'):
                # A dead zone mid-run is handled by the fail-closed feeding gate,
                # which skips it and says why. Reconnecting underneath a running
                # sequence would change its inputs halfway through.
                continue

            now = time.time()
            for plant in load_settings().get('additional_plants', []):
                record = _recovery_record(plant)
                client = plant_clients.get(plant)
                age = plant_age_seconds(plant)
                healthy = (client is not None) and client.connected and (age is not None) and (age <= PLANT_DEAD_AFTER)

                if healthy:
                    record['last_success'] = now
                    if record['healthy_since'] is None:
                        record['healthy_since'] = now
                    # Only forgive the backoff once the link has actually held. A zone
                    # that reconnects and drops again keeps its escalation, so
                    # flapping cannot produce a rebuild every sweep.
                    if record['recovery_attempts'] and (now - record['healthy_since']) >= PLANT_STABLE_AFTER:
                        log_feeding_feedback(
                            f"{plant} reconnected and stable for {PLANT_STABLE_AFTER}s", plant, status='success')
                        record['recovery_attempts'] = 0
                        record['next_attempt'] = 0
                        record['alerted'] = False
                        record['last_error'] = None
                    continue

                record['healthy_since'] = None
                if plant in _connect_in_progress:
                    continue  # startup reload (or a previous sweep) is already on it
                if now < record['next_attempt']:
                    continue  # waiting out this host's backoff; nothing else to do

                transport = None
                state = None
                if client is not None:
                    try:
                        transport = client.eio.current_transport
                        state = client.eio.state
                    except Exception:
                        pass

                first_of_episode = (record['recovery_attempts'] == 0)
                record['recovery_attempts'] += 1
                # Schedule the next eligible attempt BEFORE trying, so the spacing
                # holds whether this attempt fails outright or connects and dies.
                step = min(record['recovery_attempts'] - 1, len(PLANT_RETRY_SCHEDULE) - 1)
                record['next_attempt'] = now + PLANT_RETRY_SCHEDULE[step]

                # Log the first attempt of an outage in full, then stay quiet until it
                # recovers - a zone that is off for a day must not fill the log.
                if first_of_episode:
                    log_feeding_feedback(
                        f"Watchdog rebuilding {plant} (client={'none' if client is None else client.connected}, "
                        f"age={'never' if age is None else round(age, 1)}s, eio_state={state}, transport={transport})",
                        plant, status='warning')

                record['last_attempt'] = now
                retire_client(plant)
                if connect_to_remote_plant(plant):
                    record['total_rebuilds'] += 1
                    record['last_success'] = now
                    record['healthy_since'] = now
                    record['last_error'] = None
                else:
                    record['last_error'] = 'connect_failed'

                if record['recovery_attempts'] == PLANT_ALERT_AFTER_FAILURES and not record['alerted']:
                    record['alerted'] = True
                    log_feeding_feedback(
                        f"{plant} not staying connected after {PLANT_ALERT_AFTER_FAILURES} attempts; "
                        f"backing off to one retry every {PLANT_RETRY_SCHEDULE[-1]}s until it returns",
                        plant, status='error')
                    send_notification(f"{plant} not staying connected after {PLANT_ALERT_AFTER_FAILURES} reconnect attempts")
        except Exception as e:
            print(f"[ERROR] Plant watchdog error: {e}")
        eventlet.sleep(PLANT_WATCHDOG_INTERVAL)

# In app.py, update broadcast_plants_status to use 'app' instead of 'current_app', and remove the 'with app.app_context():' if present

def build_plants_payload():
    """Build the aggregated plants payload sent to browsers.

    is_online is derived from the AGE of the last status_update, never from the
    cached flag: a wedged socket keeps the flag true forever while its data rots.
    """
    settings = load_settings()
    additional_plants = settings.get('additional_plants', [])

    # Resolve names outside plant_lock - resolution can shell out to avahi and must
    # never hold the lock every status handler needs.
    resolved_ips = {plant: standardize_host_ip(plant) for plant in additional_plants}

    with plant_lock:
        aggregated = {'plants': []}

        for plant_ip in additional_plants:
            resolved_ip = resolved_ips.get(plant_ip)
            age = plant_age_seconds(plant_ip)
            if age is None:
                data_status = 'offline'
            elif age <= PLANT_STALE_AFTER:
                data_status = 'live'
            else:
                data_status = 'stale'

            if plant_ip in plant_data and plant_data[plant_ip].get('last_update'):
                entry = plant_data[plant_ip]
                entry['ip'] = resolved_ip or plant_ip
                entry['original_host'] = plant_ip  # Add original_host
                entry['data_status'] = data_status
                entry['last_update_age_s'] = round(age, 1) if age is not None else None
                entry['is_online'] = (data_status == 'live')
                entry['is_stale'] = (data_status == 'stale')
                aggregated['plants'].append(entry)
            else:
                aggregated['plants'].append({
                    'ip': resolved_ip or plant_ip,
                    'system_name': 'Offline',
                    'plant_name': 'N/A',
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
                    'is_online': False,
                    'is_stale': False,
                    'data_status': 'offline',
                    'last_update_age_s': None,
                    'original_host': plant_ip  # Add original_host
                })

        # Set is_currently_feeding based on current_plant_ip
        current_plant = app.config.get('current_plant_ip')
        for p in aggregated['plants']:
            p['is_currently_feeding'] = p['original_host'] == current_plant

        return aggregated


def broadcast_plants_now():
    """Push a plants_update immediately instead of waiting for the 5s loop."""
    payload = build_plants_payload()
    socketio.emit('plants_update', payload, namespace='/status')
    return payload


def broadcast_plants_status():
    while True:
        try:
            current_data = build_plants_payload()
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
        print("[INIT] Starting plants heartbeat thread...")
        eventlet.spawn(plant_heartbeat_loop)
        print("[INIT] Starting plants watchdog thread...")
        eventlet.spawn(plant_watchdog)
        print("[INIT] Starting feed mixing monitor thread...")
        eventlet.spawn(monitor_feed_mixing, socketio, app)  # Pass socketio and app
        print("[INIT] Starting daily log prune thread...")
        eventlet.spawn(prune_logs_daily)
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

@app.route('/nutrient_calculator')
def nutrient_calculator():
    return render_template('nutrient_calculator.html')

if __name__ == "__main__":
    # Resolve host for socketio.run to handle mDNS
    host = standardize_host_ip("0.0.0.0") or "0.0.0.0"
    socketio.run(app, host=host, port=8001, debug=True)