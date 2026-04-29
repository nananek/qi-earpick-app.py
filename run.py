"""Dev entry point.

    QIPOWER_IP=192.168.5.1 python run.py
"""
from app import create_app

app = create_app()


if __name__ == "__main__":
    import os
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    # Threaded but no reloader: the reloader spawns two processes, which would
    # double-open the device sockets and confuse the camera (1-client AP).
    app.run(host=host, port=port, threaded=True, use_reloader=False)
