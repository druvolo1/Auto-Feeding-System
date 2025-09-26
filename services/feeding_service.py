from app import plant_data, plant_lock  # Reference globals from app.py for data access

def validate_feeding_allowed(plant_ip):
    with plant_lock:
        if plant_ip in plant_data and plant_data[plant_ip].get('settings', {}).get('allow_remote_feeding', False):
            return True
        return False

# Placeholder for future logic, e.g., coordinate local pumps/valves with remote feeding
def initiate_local_feeding_support(plant_ip):
    pass  # Expand later if central monitor needs to pump nutrients during remote feeding