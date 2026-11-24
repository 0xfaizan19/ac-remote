"""Network probe: which ways of sending to the AC does macOS allow? (sends only harmless 'scan' packets)"""
import asyncio
import socket
import sys

if len(sys.argv) < 2:
    sys.exit("Usage: .venv/bin/python tests/net_probe.py <the AC's IP address>")
IP = sys.argv[1]
MSG = b'{"t":"scan"}'


def probe(label, fn):
    try:
        print(f"{label}: {fn()}")
    except Exception as e:  # noqa: BLE001
        print(f"{label}: FAILED {type(e).__name__}: {e}")


def unconnected():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    s.sendto(MSG, (IP, 7000)); d, a = s.recvfrom(4096); return f"OK reply {len(d)} bytes from {a}"


def connected():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    s.connect((IP, 7000)); s.send(MSG); d = s.recv(4096); return f"OK reply {len(d)} bytes"


def bcast_then_connected():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.connect((IP, 7000)); s.send(MSG); d = s.recv(4096); return f"OK reply {len(d)} bytes"


async def asyncio_remote():
    loop = asyncio.get_running_loop(); fut = loop.create_future()

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, d, a):
            fut.done() or fut.set_result(len(d))

        def error_received(self, e):
            fut.done() or fut.set_exception(e)

    t, _ = await loop.create_datagram_endpoint(P, remote_addr=(IP, 7000))
    t.sendto(MSG)
    n = await asyncio.wait_for(fut, 3); t.close(); return f"OK reply {n} bytes"

print("Python", sys.version.split()[0], "-> AC", IP)
probe("1 unconnected sendto      ", unconnected)
probe("2 connected send          ", connected)
probe("3 broadcast-flag connected", bcast_then_connected)
probe("4 asyncio remote_addr     ", lambda: asyncio.run(asyncio_remote()))
