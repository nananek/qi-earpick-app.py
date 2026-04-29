"""Flask routes for the QiPower viewer/recorder."""
from __future__ import annotations

import queue
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
)

bp = Blueprint("qipower", __name__)


def _client():
    return current_app.config["QIPOWER_CLIENT"]


def _recorder():
    return current_app.config["QIPOWER_RECORDER"]


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/stream")
def stream():
    client = _client()
    q = client.subscribe()
    boundary = b"--qipower"

    def gen():
        try:
            while True:
                try:
                    jpeg = q.get(timeout=10)
                except queue.Empty:
                    return
                yield b"".join([
                    boundary, b"\r\n",
                    b"Content-Type: image/jpeg\r\n",
                    b"Content-Length: ", str(len(jpeg)).encode(), b"\r\n\r\n",
                    jpeg, b"\r\n",
                ])
        finally:
            client.unsubscribe(q)

    return Response(
        gen(),
        mimetype="multipart/x-mixed-replace; boundary=qipower",
        headers={"Cache-Control": "no-store"},
    )


@bp.route("/snapshot.jpg")
def snapshot():
    jpeg, _ = _client().latest_frame()
    if jpeg is None:
        abort(503, "no frame available yet")
    return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@bp.route("/api/status")
def api_status():
    return jsonify({
        "client": _client().stats(),
        "recording": _recorder().status(),
    })


@bp.route("/api/reconnect", methods=["POST"])
def api_reconnect():
    _client().reconnect()
    return jsonify(_client().stats())


@bp.route("/api/battery")
def api_battery():
    info = _client().get_battery()
    if info is None:
        abort(504, "no battery reply")
    return jsonify(info)


@bp.route("/api/led", methods=["POST"])
def api_led():
    body = request.get_json(silent=True) or {}
    on = bool(body.get("on"))
    _client().set_led(on)
    return jsonify({"on": on})


@bp.route("/api/brightness", methods=["POST"])
def api_brightness():
    body = request.get_json(silent=True) or {}
    try:
        value = int(body.get("value", 0))
    except (TypeError, ValueError):
        abort(400, "value must be an int")
    _client().set_brightness(value)
    return jsonify({"value": max(0, min(100, value))})


@bp.route("/api/recordings", methods=["GET"])
def api_list_recordings():
    return jsonify({"recordings": _recorder().list()})


@bp.route("/api/recordings/start", methods=["POST"])
def api_record_start():
    rec = _recorder()
    if rec.is_recording():
        abort(409, "already recording")
    info = rec.start()
    return jsonify(info)


@bp.route("/api/recordings/stop", methods=["POST"])
def api_record_stop():
    info = _recorder().stop()
    if info is None:
        abort(409, "not recording")
    return jsonify(info)


@bp.route("/api/recordings/<name>/preview.jpg")
def api_recording_preview(name: str):
    jpeg = _recorder().first_frame(name)
    if jpeg is None:
        abort(404)
    return Response(jpeg, mimetype="image/jpeg")


@bp.route("/api/recordings/<name>/download")
def api_recording_download(name: str):
    p: Path | None = _recorder().file_path(name)
    if p is None:
        abort(404)
    return send_file(p, as_attachment=True, download_name=f"{name}.mjpeg",
                     mimetype="video/x-motion-jpeg")
