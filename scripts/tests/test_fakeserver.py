"""The fake-server helper's teardown finishes when a connection is dropped
before `accept()` (issue 708)."""

from __future__ import annotations

import selectors
import socketserver
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fakeserver
import pytest
from fakeserver import serve, stop


class _AlwaysReadable(selectors.DefaultSelector):
    """Reports the listen socket readable with nothing queued: what
    `serve_forever` sees when a client goes away between the poll and the
    `accept()`."""

    def select(self, timeout=None):
        time.sleep(0.01)
        return [(key, selectors.EVENT_READ) for key in self.get_map().values()]


def test_teardown_finishes_when_the_connection_drops_before_accept(monkeypatch):
    monkeypatch.setattr(socketserver, "_ServerSelector", _AlwaysReadable)
    server = serve(ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler))
    time.sleep(0.2)  # let the loop reach accept() with an empty queue
    stopper = threading.Thread(target=stop, args=(server,), daemon=True)
    stopper.start()
    stopper.join(timeout=fakeserver.ACCEPT_TIMEOUT * 5)
    assert not stopper.is_alive(), "stop() hung behind a blocking accept()"
    with pytest.raises(OSError):
        server.socket.getsockname()  # server_close() closed the listen socket


def test_accepted_connections_stay_blocking():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.connection.gettimeout())
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5)
    finally:
        stop(server)
    assert seen == [None]
