"""Small HTTP service and TCP dependency for isolated fault-injection demos."""
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROLE = os.getenv("ROLE", "api")
PORT = int(os.getenv("PORT", "8000"))


def dependency_ready():
    try:
        with socket.create_connection((os.getenv("DB_HOST", "db"), 5432), timeout=1):
            return True
    except OSError as exc:
        print(f"database connection failed: {exc}", flush=True)
        return False


if ROLE == "db":
    time.sleep(int(os.getenv("DB_READY_DELAY", "0")))
    with socket.create_server(("0.0.0.0", 5432)) as server:
        print("TCP dependency ready on 5432", flush=True)
        while True:
            connection, _ = server.accept()
            connection.close()
else:
    if os.getenv("REQUIRE_DB_AT_BOOT") == "1" and not dependency_ready():
        raise SystemExit("database connection refused: dependency not ready")

    def exhaust_memory():
        time.sleep(5)
        blocks = []
        while True:
            blocks.append(bytearray(4 * 1024 * 1024))
            print(f"allocated {len(blocks) * 4} MiB", flush=True)
            time.sleep(.1)

    if os.getenv("INJECT_OOM") == "1":
        threading.Thread(target=exhaust_memory, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ready = ROLE == "web" or dependency_ready()
            self.send_response(200 if ready else 503)
            self.end_headers()
            self.wfile.write(b"healthy" if ready else b"dependency unavailable")

    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
