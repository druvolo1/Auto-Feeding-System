from flask_socketio import Namespace

_socketio = None

def set_socketio_instance(sio):
    global _socketio
    _socketio = sio

class StatusNamespace(Namespace):
    def on_connect(self, auth=None):
        print("Client connected to /status")

    def on_disconnect(self):
        print("Client disconnected from /status")