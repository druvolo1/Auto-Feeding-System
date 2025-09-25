import requests
import json

def control_feed_pump(ip, pump_type, state=None, get_status=False):
    """
    Control or query the state of a Kasa or Shelly smart plug.
    For now, only Kasa is implemented.
    Args:
        ip (str): IP address of the smart plug.
        pump_type (str): 'kasa' or 'shelly'.
        state (int, optional): 1 to turn on, 0 to turn off. Omit for status query.
        get_status (bool): If True, return the current state instead of setting it.
    Returns:
        bool or int: True/False for control success, 1/0 for status (on/off).
    """
    url = f"http://{ip}:9999"  # Kasa local API port

    if pump_type != 'kasa':
        raise ValueError("Only Kasa support is implemented currently")

    try:
        if get_status:
            response = requests.post(url, json={"method": "get_sysinfo"})
            if response.status_code == 200:
                data = response.json()
                if data.get("error_code") == 0:
                    return data["result"]["relay_state"]  # 1 = on, 0 = off
            return None  # Failed to get status

        payload = {"method": "set_relay_state", "params": {"state": state}}
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            data = response.json()
            return data.get("error_code") == 0  # True if successful
        return False  # Failed to control
    except requests.RequestException as e:
        raise Exception(f"Network error: {str(e)}")
    except json.JSONDecodeError:
        raise Exception("Invalid response from device")