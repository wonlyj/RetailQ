from contextlib import contextmanager
import socket
import threading
import time

import uvicorn


@contextmanager
def run_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test server failed to start")
            time.sleep(0.01)
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=2)
            raise RuntimeError("test server failed to stop")
