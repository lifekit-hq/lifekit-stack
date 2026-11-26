"""Start and stop the suite's in-process fake HTTP/TCP servers (issue 708).

`serve_forever` polls the listen socket, then calls `accept()`. With a blocking
listen socket, a client that drops its connection between that poll and the
`accept()` leaves the server thread stuck in `accept()` for good, and the
fixture's `shutdown()` waits on it forever. A timeout on the listen socket
turns that into a `TimeoutError`, which `socketserver` swallows before it
checks for shutdown again. Accepted connections stay blocking: `accept()`
hands them back in blocking mode when no global default timeout is set.
"""

from __future__ import annotations

import socketserver
import threading

ACCEPT_TIMEOUT = 1.0


def serve(server: socketserver.BaseServer) -> socketserver.BaseServer:
    """Run `server` on a daemon thread; its `accept()` can never block forever."""
    server.socket.settimeout(ACCEPT_TIMEOUT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(server: socketserver.BaseServer) -> None:
    """Stop the serve loop and close the listen socket."""
    server.shutdown()
    server.server_close()
