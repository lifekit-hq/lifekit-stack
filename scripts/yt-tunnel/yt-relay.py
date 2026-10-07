#!/usr/bin/env python3
"""Allow-list SOCKS5 relay in front of the yt-tunnel ssh forward.

skills/youtube-transcript talks to this relay, never to the ssh -D listener.
The relay is the only thing listening on the docker bridge address; the ssh
forward (an open SOCKS proxy into the owner's PC network) binds loopback only,
which no container can reach. The relay speaks SOCKS5 to its client: no auth,
CONNECT only, domain-name addresses only, and only for youtube.com,
googlevideo.com, ytimg.com and their subdomains. Everything else, IP literals
included, is refused with reply 0x02 and a log line. An allowed name goes on to
the ssh forward still unresolved (socks5h), so the PC resolves it; an upstream
that is down or stuck answers the client with reply 0x05.

    yt-relay.py --listen HOST:PORT --upstream HOST:PORT
"""

from __future__ import annotations

import argparse
import asyncio
import re
import struct
import sys

ALLOWED = ("youtube.com", "googlevideo.com", "ytimg.com")
HOSTNAME = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*"
)
HANDSHAKE_TIMEOUT = 10.0
UPSTREAM_TIMEOUT = 6.0
IDLE_TIMEOUT = 120.0

NOT_ALLOWED = 0x02
CONNECTION_REFUSED = 0x05


class Refused(Exception):
    pass


class NoMethod(Refused):
    pass


def allowed(name: str) -> bool:
    n = name.lower().rstrip(".")
    return bool(HOSTNAME.fullmatch(n)) and any(
        n == d or n.endswith("." + d) for d in ALLOWED
    )


def reply(code: int) -> bytes:
    return bytes([5, code, 0, 1]) + bytes(6)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def negotiate(
    r: asyncio.StreamReader, w: asyncio.StreamWriter
) -> tuple[str, int]:
    ver, nmethods = await r.readexactly(2)
    methods = await r.readexactly(nmethods)
    if ver != 5 or 0 not in methods:
        raise NoMethod("no usable auth method")
    w.write(b"\x05\x00")
    ver, cmd, _, atyp = await r.readexactly(4)
    if ver != 5 or cmd != 1:
        raise Refused(f"command {cmd} (CONNECT only)")
    if atyp != 3:
        raise Refused(f"address type {atyp} (domain names only)")
    name = (await r.readexactly((await r.readexactly(1))[0])).decode("latin-1")
    (port,) = struct.unpack(">H", await r.readexactly(2))
    if not allowed(name):
        raise Refused(f"{name!r} is not on the allow-list")
    return name, port


async def connect_upstream(
    upstream: tuple[str, int], name: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_connection(*upstream)
    try:
        w.write(b"\x05\x01\x00")
        if await r.readexactly(2) != b"\x05\x00":
            raise ConnectionError("upstream wants authentication")
        nb = name.lower().rstrip(".").encode("ascii")
        w.write(b"\x05\x01\x00\x03" + bytes([len(nb)]) + nb + struct.pack(">H", port))
        _, rep, _, atyp = await r.readexactly(4)
        if rep != 0:
            raise ConnectionError(f"upstream replied {rep}")
        skip = {1: 6, 4: 18}.get(atyp)
        await r.readexactly(skip if skip else (await r.readexactly(1))[0] + 2)
    except BaseException:
        w.close()
        raise
    return r, w


async def pump(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await asyncio.wait_for(r.read(65536), IDLE_TIMEOUT):
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass


async def handle(
    cr: asyncio.StreamReader, cw: asyncio.StreamWriter, upstream: tuple[str, int]
) -> None:
    peer = cw.get_extra_info("peername")
    ur = uw = None
    try:
        try:
            name, port = await asyncio.wait_for(negotiate(cr, cw), HANDSHAKE_TIMEOUT)
        except Refused as e:
            log(f"refused {peer}: {e}")
            cw.write(b"\x05\xff" if isinstance(e, NoMethod) else reply(NOT_ALLOWED))
            return
        try:
            ur, uw = await asyncio.wait_for(
                connect_upstream(upstream, name, port), UPSTREAM_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            log(f"upstream failed for {name}:{port}: {e!r}")
            cw.write(reply(CONNECTION_REFUSED))
            return
        cw.write(reply(0))
        await cw.drain()
        await asyncio.gather(pump(cr, uw), pump(ur, cw))
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        for s in (cw, uw):
            if s is not None:
                s.close()


def hostport(s: str) -> tuple[str, int]:
    host, _, port = s.rpartition(":")
    return host, int(port)


async def serve(listen: tuple[str, int], upstream: tuple[str, int]) -> None:
    server = await asyncio.start_server(lambda r, w: handle(r, w, upstream), *listen)
    host, port = server.sockets[0].getsockname()[:2]
    print(f"listening {host}:{port}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", required=True, type=hostport)
    ap.add_argument("--upstream", required=True, type=hostport)
    args = ap.parse_args()
    try:
        asyncio.run(serve(args.listen, args.upstream))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
