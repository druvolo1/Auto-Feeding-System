from flask import Blueprint, jsonify, request
from utils.settings_utils import load_settings, save_settings
from services.feed_pump_service import control_feed_pump
from services.feeding_service import log_feeding_feedback, send_notification
import logging
import uuid

feed_pump_blueprint = Blueprint('feed_pump', __name__)
logger = logging.getLogger(__name__)

@feed_pump_blueprint.route('/on', methods=['POST'])
def turn_on_feed_pump():
    request_id = str(uuid.uuid4())
    logger.debug(f"Processing turn_on_feed_pump request {request_id}")
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    io_number = feed_pump.get('io_number')
    pump_type = feed_pump.get('type', 'io')

    if pump_type == 'io' and not io_number:
        log_feeding_feedback("Feed pump IO number not configured", status='error')
        send_notification("Feed pump IO number not configured")
        return jsonify({"status": "failure", "error": "Feed pump IO number not configured"}), 400
    elif pump_type == 'shelly' and not feed_pump.get('ip'):
        log_feeding_feedback("Feed pump IP not configured for Shelly", status='error')
        send_notification("Feed pump IP not configured for Shelly")
        return jsonify({"status": "failure", "error": "Feed pump IP not configured for Shelly"}), 400

    try:
        logger.debug(f"Calling control_feed_pump with io_number={io_number}, pump_type={pump_type}, state=1 for request {request_id}")
        success = control_feed_pump(io_number=io_number, pump_type=pump_type, state=1)
        if success:
            logger.debug(f"Successfully turned on feed pump for request {request_id}")
            return jsonify({"status": "success"})
        else:
            logger.debug(f"Failed to turn on feed pump for request {request_id}")
            return jsonify({"status": "failure", "error": "Failed to turn on feed pump"}), 500
    except Exception as e:
        logger.error(f"Error in turn_on_feed_pump for request {request_id}: {str(e)}")
        log_feeding_feedback(f"Error turning on feed pump: {str(e)}", status='error')
        send_notification(f"Error turning on feed pump: {str(e)}")
        return jsonify({"status": "failure", "error": str(e)}), 500

@feed_pump_blueprint.route('/off', methods=['POST'])
def turn_off_feed_pump():
    request_id = str(uuid.uuid4())
    logger.debug(f"Processing turn_off_feed_pump request {request_id}")
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    io_number = feed_pump.get('io_number')
    pump_type = feed_pump.get('type', 'io')

    if pump_type == 'io' and not io_number:
        log_feeding_feedback("Feed pump IO number not configured", status='error')
        send_notification("Feed pump IO number not configured")
        return jsonify({"status": "failure", "error": "Feed pump IO number not configured"}), 400
    elif pump_type == 'shelly' and not feed_pump.get('ip'):
        log_feeding_feedback("Feed pump IP not configured for Shelly", status='error')
        send_notification("Feed pump IP not configured for Shelly")
        return jsonify({"status": "failure", "error": "Feed pump IP not configured for Shelly"}), 400

    try:
        logger.debug(f"Calling control_feed_pump with io_number={io_number}, pump_type={pump_type}, state=0 for request {request_id}")
        success = control_feed_pump(io_number=io_number, pump_type=pump_type, state=0)
        if success:
            logger.debug(f"Successfully turned off feed pump for request {request_id}")
            return jsonify({"status": "success"})
        else:
            logger.debug(f"Failed to turn off feed pump for request {request_id}")
            return jsonify({"status": "failure", "error": "Failed to turn off feed pump"}), 500
    except Exception as e:
        logger.error(f"Error in turn_off_feed_pump for request {request_id}: {str(e)}")
        log_feeding_feedback(f"Error turning off feed pump: {str(e)}", status='error')
        send_notification(f"Error turning off feed pump: {str(e)}")
        return jsonify({"status": "failure", "error": str(e)}), 500

@feed_pump_blueprint.route('/status', methods=['GET'])
def get_feed_pump_status():
    request_id = str(uuid.uuid4())
    logger.debug(f"Processing get_feed_pump_status request {request_id}")
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    io_number = feed_pump.get('io_number')
    pump_type = feed_pump.get('type', 'io')

    if pump_type == 'io' and not io_number:
        log_feeding_feedback("Feed pump IO number not configured", status='error')
        send_notification("Feed pump IO number not configured")
        return jsonify({"status": "failure", "error": "Feed pump IO number not configured"}), 400
    elif pump_type == 'shelly' and not feed_pump.get('ip'):
        log_feeding_feedback("Feed pump IP not configured for Shelly", status='error')
        send_notification("Feed pump IP not configured for Shelly")
        return jsonify({"status": "failure", "error": "Feed pump IP not configured for Shelly"}), 400

    try:
        logger.debug(f"Calling control_feed_pump with io_number={io_number}, pump_type={pump_type}, get_status=True for request {request_id}")
        status = control_feed_pump(io_number=io_number, pump_type=pump_type, get_status=True)
        logger.debug(f"Got feed pump status {status} for request {request_id}")
        return jsonify({"status": "success", "state": "on" if status else "off"})
    except Exception as e:
        logger.error(f"Error in get_feed_pump_status for request {request_id}: {str(e)}")
        log_feeding_feedback(f"Error getting feed pump status: {str(e)}", status='error')
        send_notification(f"Error getting feed pump status: {str(e)}")
        return jsonify({"status": "failure", "error": str(e)}), 500