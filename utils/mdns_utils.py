import socket
import subprocess
from utils.settings_utils import load_settings
import logging

# Set up basic logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def get_local_ip_address():
    """
    Return this Pi’s primary LAN IP, or '127.0.0.1' on fallback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        logger.debug(f"Resolved local IP: {ip}")
        return ip
    except Exception as e:
        logger.error(f"Failed to get local IP: {e}")
        return "127.0.0.1"
    finally:
        s.close()

def resolve_mdns(hostname: str) -> str:
    """
    Tries to resolve a .local hostname via:
      1) avahi-resolve-host-name -4 <hostname>
      2) socket.getaddrinfo()
      3) socket.gethostbyname()
    Returns the resolved IP string, or None if resolution fails.
    """
    if not hostname:
        logger.debug("Hostname is empty, returning None")
        return None

    logger.debug(f"Attempting to resolve hostname: {hostname}")

    # If it's NOT a .local name, skip avahi and do getaddrinfo() + gethostbyname().
    if not hostname.endswith(".local"):
        ip = fallback_socket_resolve(hostname)
        if ip:
            logger.debug(f"Resolved {hostname} via getaddrinfo: {ip}")
            return ip
        # Now fallback to gethostbyname:
        try:
            ip = socket.gethostbyname(hostname)
            logger.debug(f"Resolved {hostname} via gethostbyname: {ip}")
            return ip
        except Exception as e:
            logger.error(f"gethostbyname failed for {hostname}: {e}")
            return None

    # If it IS a .local, try avahi first:
    try:
        logger.debug(f"Attempting avahi-resolve-host-name for {hostname}")
        result = subprocess.run(
            ["avahi-resolve-host-name", "-4", hostname],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            ip_address = result.stdout.strip().split()[-1]
            logger.debug(f"Resolved {hostname} via avahi: {ip_address}")
            return ip_address
        else:
            logger.warning(f"avahi-resolve-host-name failed or returned no output for {hostname}: {result.stderr}")
    except Exception as e:
        logger.error(f"avahi-resolve-host-name error for {hostname}: {e}")
        pass

    # Then fallback to socket.getaddrinfo():
    ip = fallback_socket_resolve(hostname)
    if ip:
        logger.debug(f"Resolved {hostname} via getaddrinfo: {ip}")
        return ip

    # Finally, fallback to socket.gethostbyname():
    try:
        ip = socket.gethostbyname(hostname)
        logger.debug(f"Resolved {hostname} via gethostbyname: {ip}")
        return ip
    except Exception as e:
        logger.error(f"gethostbyname failed for {hostname}: {e}")
        return None

def fallback_socket_resolve(hostname: str) -> str:
    """
    A helper that tries socket.getaddrinfo() for an IPv4 address.
    """
    try:
        logger.debug(f"Attempting getaddrinfo for {hostname}")
        info = socket.getaddrinfo(hostname, None, socket.AF_INET)
        if info:
            ip = info[0][4][0]
            logger.debug(f"getaddrinfo resolved {hostname} to {ip}")
            return ip
    except Exception as e:
        logger.error(f"getaddrinfo failed for {hostname}: {e}")
        pass
    return None

def standardize_host_ip(raw_host_ip: str) -> str:
    """
    If raw_host_ip is empty, or 'localhost', '127.0.0.1', or '<system_name>.local',
    replace with this Pi’s LAN IP. If .local is anything else, try mDNS lookup.
    Otherwise return raw_host_ip unchanged.
    """
    if not raw_host_ip:
        logger.debug("raw_host_ip is empty, returning None")
        return None

    settings = load_settings()
    system_name = settings.get("system_name", "Garden").lower()
    lower_host = raw_host_ip.lower()

    logger.debug(f"Standardizing host IP for {raw_host_ip}, system_name: {system_name}")

    # If local host or system_name.local, replace with local IP
    if lower_host in ["localhost", "127.0.0.1", f"{system_name}.local"]:
        ip = get_local_ip_address()
        logger.debug(f"Replaced {lower_host} with local IP: {ip}")
        return ip

    # If any other .local, resolve via mDNS
    if lower_host.endswith(".local"):
        resolved = resolve_mdns(lower_host)
        if resolved:
            logger.debug(f"Resolved {lower_host} to {resolved}")
            return resolved
        logger.warning(f"Failed to resolve {lower_host} via mDNS")

    # If not .local, or resolution failed, just return as-is
    logger.debug(f"Returning {raw_host_ip} unchanged")
    return raw_host_ip