from flask import current_app

def validate_feeding_allowed(plant_ip):
    with current_app.config['plant_lock']:
        plant_data = current_app.config['plant_data']
        if plant_ip in plant_data and plant_data[plant_ip].get('settings', {}).get('allow_remote_feeding', False):
            return True
        return False

def initiate_local_feeding_support(plant_ip):
    pass  # Placeholder for future logic, e.g., coordinate local pumps/valves