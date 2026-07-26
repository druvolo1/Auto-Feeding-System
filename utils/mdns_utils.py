import socket
import subprocess
import time
from utils.settings_utils import load_settings
import logging
import os

# Set up basic logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Short-lived resolution cache. Resolution happens on every broadcast tick and every
# valve call; without this a slow avahi turns into a per-call subprocess spawn.
_RESOLVE_CACHE = {}          # hostname -> (ip_or_None, expires_at)
_RESOLVE_TTL_OK = 60.0       # seconds to trust a successful lookup
_RESOLVE_TTL_FAIL = 10.0     # seconds before retrying a failed lookup
AVAHI_TIMEOUT = 3            # seconds; avahi-resolve-host-name can otherwise hang


def _cache_get(hostname):
    entry = _RESOLVE_CACHE.get(hostname)
    if entry and entry[1] > time.time():
        return True, entry[0]
    return False, None


def _cache_put(hostname, ip):
    ttl = _RESOLVE_TTL_OK if ip else _RESOLVE_TTL_FAIL
    _RESOLVE_CACHE[hostname] = (ip, time.time() + ttl)
    return ip

def get_local_ip_address():
    """
    Return this Pi’s primary LAN IP, or '127.0.0.1' on fallback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug(f"Resolved local IP: {ip}")
        return ip
    except Exception as e:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.error(f"Failed to get local IP: {e}")
        return "127.0.0.1"
    finally:
        s.close()

def resolve_mdns(hostname: str) -> str:
    """
    Tries to resolve a .local hostname via:
      1) /usr/bin/avahi-resolve-host-name -4 <hostname>
      2) socket.getaddrinfo()
      3) socket.gethostbyname()
    Returns the resolved IP string, or None if resolution fails.
    """
    if not hostname:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug("Hostname is empty, returning None")
        return None

    if load_settings().get('debug_states', {}).get('dns-resolution', False):
        logger.debug(f"Attempting to resolve hostname: {hostname}")

    cached, cached_ip = _cache_get(hostname)
    if cached:
        return cached_ip

    # If it's NOT a .local name, skip avahi and do getaddrinfo() + gethostbyname().
    if not hostname.endswith(".local"):
        ip = fallback_socket_resolve(hostname)
        if ip and load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug(f"Resolved {hostname} via getaddrinfo: {ip}")
        if not ip:
            try:
                ip = socket.gethostbyname(hostname)
                if load_settings().get('debug_states', {}).get('dns-resolution', False):
                    logger.debug(f"Resolved {hostname} via gethostbyname: {ip}")
            except Exception as e:
                if load_settings().get('debug_states', {}).get('dns-resolution', False):
                    logger.error(f"gethostbyname failed for {hostname}: {e}")
        return _cache_put(hostname, ip)

    # If it IS a .local, try avahi first:
    try:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug(f"Attempting /usr/bin/avahi-resolve-host-name for {hostname}")
        if os.path.exists("/usr/bin/avahi-resolve-host-name"):
            result = subprocess.run(
                ["/usr/bin/avahi-resolve-host-name", "-4", hostname],
                capture_output=True,
                text=True,
                check=False,
                timeout=AVAHI_TIMEOUT
            )
            if result.returncode == 0 and result.stdout.strip():
                ip_address = result.stdout.strip().split()[-1]
                if load_settings().get('debug_states', {}).get('dns-resolution', False):
                    logger.debug(f"Resolved {hostname} via avahi: {ip_address}")
                return _cache_put(hostname, ip_address)
            else:
                if load_settings().get('debug_states', {}).get('dns-resolution', False):
                    logger.warning(f"/usr/bin/avahi-resolve-host-name failed or returned no output for {hostname}: {result.stderr}")
        else:
            if load_settings().get('debug_states', {}).get('dns-resolution', False):
                logger.error(f"/usr/bin/avahi-resolve-host-name does not exist")
    except subprocess.TimeoutExpired:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.error(f"/usr/bin/avahi-resolve-host-name timed out after {AVAHI_TIMEOUT}s for {hostname}")
    except Exception as e:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.error(f"/usr/bin/avahi-resolve-host-name error for {hostname}: {e}")
        pass

    # Then fallback to socket.getaddrinfo():
    ip = fallback_socket_resolve(hostname)
    if ip and load_settings().get('debug_states', {}).get('dns-resolution', False):
        logger.debug(f"Resolved {hostname} via getaddrinfo: {ip}")
    if not ip:
        try:
            ip = socket.gethostbyname(hostname)
            if load_settings().get('debug_states', {}).get('dns-resolution', False):
                logger.debug(f"Resolved {hostname} via gethostbyname: {ip}")
        except Exception as e:
            if load_settings().get('debug_states', {}).get('dns-resolution', False):
                logger.error(f"gethostbyname failed for {hostname}: {e}")
    return _cache_put(hostname, ip)

def fallback_socket_resolve(hostname: str) -> str:
    """
    A helper that tries socket.getaddrinfo() for an IPv4 address.
    """
    try:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug(f"Attempting getaddrinfo for {hostname}")
        info = socket.getaddrinfo(hostname, None, socket.AF_INET)
        if info:
            ip = info[0][4][0]
            if load_settings().get('debug_states', {}).get('dns-resolution', False):
                logger.debug(f"getaddrinfo resolved {hostname} to {ip}")
            return ip
    except Exception as e:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.error(f"getaddrinfo failed for {hostname}: {e}")
        pass
    return None

def standardize_host_ip(raw_host_ip: str) -> str:
    """
    If raw_host_ip is empty, or 'localhost', '127.0.0.1', or '<system_name>.local',
    replace with this Pi's LAN IP. If .local is anything else, try mDNS lookup.
    Otherwise return raw_host_ip unchanged.
    """
    if not raw_host_ip:
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug("raw_host_ip is empty, returning None")
        return None

    settings = load_settings()
    system_name = settings.get("system_name", "Garden").lower()
    lower_host = raw_host_ip.lower()

    if load_settings().get('debug_states', {}).get('dns-resolution', False):
        logger.debug(f"Standardizing host IP for {raw_host_ip}, system_name: {system_name}")

    # If local host or system_name.local, replace with local IP
    if lower_host in ["localhost", "127.0.0.1", f"{system_name}.local"]:
        ip = get_local_ip_address()
        if load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug(f"Replaced {lower_host} with local IP: {ip}")
        return ip

    # If any other .local, resolve via mDNS
    if lower_host.endswith(".local"):
        resolved = resolve_mdns(lower_host)
        if resolved and load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.debug(f"Resolved {lower_host} to {resolved}")
        if not resolved and load_settings().get('debug_states', {}).get('dns-resolution', False):
            logger.warning(f"Failed to resolve {lower_host} via mDNS")
        return resolved

    # If not .local, or resolution failed, just return as-is
    if load_settings().get('debug_states', {}).get('dns-resolution', False):
        logger.debug(f"Returning {raw_host_ip} unchanged")
    return raw_host_ip