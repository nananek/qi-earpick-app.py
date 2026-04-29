"""QiPower Flask viewer & recorder."""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path

from flask import Flask

from .qipower import QiPowerClient
from .recording import RecordingManager
from .views import bp


def create_app() -> Flask:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = Flask(__name__)

    device_ip = os.environ.get("QIPOWER_IP", "192.168.5.1")
    rec_dir = Path(os.environ.get("QIPOWER_REC_DIR", "recordings")).resolve()

    client = QiPowerClient(ip=device_ip)
    client.start()
    atexit.register(client.stop)

    app.config["QIPOWER_CLIENT"] = client
    app.config["QIPOWER_RECORDER"] = RecordingManager(client, rec_dir)
    app.register_blueprint(bp)
    return app
