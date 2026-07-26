from flask import Blueprint, jsonify, request, current_app
from datetime import datetime
import requests

from services.log_service import log_event
from utils.mdns_utils import standardize_host_ip
from utils.settings_utils import load_settings

plants_blueprint = Blueprint('plants', __name__)

ZONE_TIMEOUT = 5  # seconds for any HTTP call to a zone controller


def log_plant_feedback(message, plant_ip=None, status='info'):
    """Mirror the feeding feedback pane so remote-feeding changes are auditable."""
    socketio = current_app.extensions['socketio']
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


def _cache_allow_remote_feeding(host, value):
    """Write the authoritative value straight into the cached plant payload so the
    UI and the feeding gate stop showing the pre-change value."""
    with current_app.config['plant_lock']:
        entry = current_app.config['plant_data'].get(host)
        if entry is not None:
            entry.setdefault('settings', {})['allow_remote_feeding'] = value


def _request_refresh(host):
    """Nudge the zone to re-emit a full status payload, if its socket is alive."""
    client = current_app.config.get('plant_clients', {}).get(host)
    if client is not None:
        try:
            if client.connected:
                client.emit('request_refresh', namespace='/status')
        except Exception:
            pass


@plants_blueprint.route('/allow_remote_feeding', methods=['POST'])
def set_allow_remote_feeding():
    """Set a zone's allow_remote_feeding to an ABSOLUTE value.

    Never a flip: the browser sends the value it wants, so a stale cached boolean
    can no longer invert the request. The zone's own response is authoritative.
    """
    data = request.get_json() or {}
    host = data.get('host')
    if not host:
        return jsonify({"status": "failure", "error": "Missing 'host'"}), 400
    if 'value' not in data:
        return jsonify({"status": "failure", "error": "Missing 'value'"}), 400
    value = bool(data.get('value'))

    ip = standardize_host_ip(host)
    if not ip:
        log_plant_feedback(f"Cannot resolve {host} to set remote feeding", host, status='error')
        return jsonify({"status": "failure", "error": f"Cannot resolve {host}"}), 502

    try:
        response = requests.post(
            f"http://{ip}:8000/api/plant_info/",
            json={"allow_remote_feeding": value},
            timeout=ZONE_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        log_plant_feedback(f"Failed to set remote feeding on {host}: {str(e)}", host, status='error')
        return jsonify({"status": "failure", "error": str(e)}), 502

    authoritative = bool(payload.get('settings', {}).get('allow_remote_feeding'))
    _cache_allow_remote_feeding(host, authoritative)
    _request_refresh(host)

    from app import broadcast_plants_now
    broadcast_plants_now()

    log_plant_feedback(
        f"Remote feeding for {host} set to {'enabled' if authoritative else 'disabled'}",
        host, status='success')
    return jsonify({
        "status": "success",
        "host": host,
        "allow_remote_feeding": authoritative
    })


@plants_blueprint.route('/diagnostics', methods=['GET'])
def plant_diagnostics():
    """Expose the watchdog's per-host state: how many attempts it has made, when
    the next one is due, and how long ago each zone last reported."""
    import time

    plant_data = current_app.config.get('plant_data', {})
    plant_clients = current_app.config.get('plant_clients', {})
    recovery = current_app.config.get('plant_recovery', {})
    now = time.time()

    hosts = []
    for host in load_settings().get('additional_plants', []):
        record = recovery.get(host, {})
        last_update = (plant_data.get(host) or {}).get('last_update')
        next_attempt = record.get('next_attempt') or 0
        hosts.append({
            'host': host,
            'socket_connected': bool(getattr(plant_clients.get(host), 'connected', False)),
            'last_update_age_s': None if not last_update else round(max(0.0, now - last_update / 1000.0), 1),
            'recovery_attempts': record.get('recovery_attempts', 0),
            'total_rebuilds': record.get('total_rebuilds', 0),
            'next_attempt_in_s': None if next_attempt <= now else round(next_attempt - now, 1),
            'last_error': record.get('last_error'),
        })

    return jsonify({
        "status": "success",
        "feeding_sequence_active": bool(current_app.config.get('feeding_sequence_active')),
        "watchdog_paused_for_feeding": bool(current_app.config.get('feeding_sequence_active')),
        "plants": hosts
    })


@plants_blueprint.route('/<path:host>/live', methods=['GET'])
def live_plant_settings(host):
    """Read a zone's settings directly over HTTP, bypassing the socket cache.

    Used by the UI to show the truth even when a plant's socket is stale or dead.
    """
    ip = standardize_host_ip(host)
    if not ip:
        return jsonify({"status": "failure", "error": f"Cannot resolve {host}"}), 502

    try:
        response = requests.get(f"http://{ip}:8000/api/settings/", timeout=ZONE_TIMEOUT)
        response.raise_for_status()
        settings = response.json()
    except Exception as e:
        return jsonify({"status": "failure", "host": host, "error": str(e)}), 502

    allow = bool(settings.get('allow_remote_feeding'))
    _cache_allow_remote_feeding(host, allow)

    return jsonify({
        "status": "success",
        "host": host,
        "ip": ip,
        "allow_remote_feeding": allow,
        "feeding_in_progress": bool(settings.get('feeding_in_progress')),
        "system_name": settings.get('system_name'),
        "source": "http"
    })
