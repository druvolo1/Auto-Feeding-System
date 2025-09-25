from flask import Blueprint, jsonify, request
from utils.settings_utils import load_settings, save_settings
from services.feed_pump_service import control_feed_pump
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
    pump_type = feed_pump.get('type', 'io')  # Default to 'io' if not set

    if not io_number:
        return jsonify({"status": "failure", "error": "Feed pump IO number not configured"}), 400

    try:
        logger.debug(f"Calling control_feed_pump with io_number={io_number}, pump_type={pump_type}, state=1 for request {request_id}")
        success = control_feed_pump(io_number=io_number, pump_type=pump_type, state=1)  # 1 for ON
        if success:
            logger.debug(f"Successfully turned on feed pump for request {request_id}")
            return jsonify({"status": "success"})
        else:
            logger.debug(f"Failed to turn on feed pump for request {request_id}")
            return jsonify({"status": "failure", "error": "Failed to turn on feed pump"}), 500
    except Exception as e:
        logger.error(f"Error in turn_on_feed_pump for request {request_id}: {str(e)}")
        return jsonify({"status": "failure", "error": str(e)}), 500

@feed_pump_blueprint.route('/off', methods=['POST'])
def turn_off_feed_pump():
    request_id = str(uuid.uuid4())
    logger.debug(f"Processing turn_off_feed_pump request {request_id}")
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    io_number = feed_pump.get('io_number')
    pump_type = feed_pump.get('type', 'io')  # Default to 'io' if not set

    if not io_number:
        return jsonify({"status": "failure", "error": "Feed pump IO number not configured"}), 400

    try:
        logger.debug(f"Calling control_feed_pump with io_number={io_number}, pump_type={pump_type}, state=0 for request {request_id}")
        success = control_feed_pump(io_number=io_number, pump_type=pump_type, state=0)  # 0 for OFF
        if success:
            logger.debug(f"Successfully turned off feed pump for request {request_id}")
            return jsonify({"status": "success"})
        else:
            logger.debug(f"Failed to turn off feed pump for request {request_id}")
            return jsonify({"status": "failure", "error": "Failed to turn off feed pump"}), 500
    except Exception as e:
        logger.error(f"Error in turn_off_feed_pump for request {request_id}: {str(e)}")
        return jsonify({"status": "failure", "error": str(e)}), 500

@feed_pump_blueprint.route('/status', methods=['GET'])
def get_feed_pump_status():
    request_id = str(uuid.uuid4())
    logger.debug(f"Processing get_feed_pump_status request {request_id}")
    settings = load_settings()
    feed_pump = settings.get('feed_pump', {})
    io_number = feed_pump.get('io_number')
    pump_type = feed_pump.get('type', 'io')  # Default to 'io' if not set

    if not io_number:
        return jsonify({"status": "failure", "error": "Feed pump IO number not configured"}), 400

    try:
        logger.debug(f"Calling control_feed_pump with io_number={io_number}, pump_type={pump_type}, get_status=True for request {request_id}")
        status = control_feed_pump(io_number=io_number, pump_type=pump_type, get_status=True)
        logger.debug(f"Got feed pump status {status} for request {request_id}")
        return jsonify({"status": "success", "state": status})
    except Exception as e:
        logger.error(f"Error in get_feed_pump_status for request {request_id}: {str(e)}")
        return jsonify({"status": "failure", "error": str(e)}), 500