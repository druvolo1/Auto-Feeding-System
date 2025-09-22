from flask import Blueprint, jsonify
from services.feed_level_service import get_feed_level

feed_level_blueprint = Blueprint('feed_level', __name__)

@feed_level_blueprint.route('/status', methods=['GET'])
def get_status():
    level = get_feed_level()
    return jsonify({'feed_level': level})