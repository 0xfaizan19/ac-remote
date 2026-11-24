"""AC Remote relay: passes the server's packets to the AC and back.

Why it exists: on macOS 15+, a background program needs the Local Network permission to send packets
to devices on the home network, and macOS can't reliably track that permission for unsigned programs
such as Homebrew's Python (Apple technote TN3179). Even with Python switched on under
Privacy & Security > Local Network, the server's packets to the AC fail with "Broken pipe".
Programs running as root are exempt, so this small relay runs as root, and the AC Remote server
(which runs as you) talks to the AC through it.

Kept deliberately small: standard library only (it runs on Apple's own python3), listens on
127.0.0.1 only, and forwards only to port 7000 of a private (home network) IPv4 address. That address
is read from the file named on the command line, which the server writes when it finds the AC.

Usage: /usr/bin/python3 -I relay.py /path/to/ac-remote/data/ac_target
"""
import ipaddress
import select
import socket
import sys
import time

LISTEN = ("127.0.0.1", 7001)
AC_PORT = 7000
IDLE = 600  # forget a client after 10 minutes without traffic


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def read_target(path):
    """The AC's address, if the file holds a valid home-network IPv4 address."""
    try:
        with open(path) as f:
            ip = ipaddress.IPv4Address(f.read(64).strip())
    except (OSError, ValueError):
        return None
    if ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        return str(ip)
    return None


def main():
    target_file = sys.argv[1]
    lsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    lsock.bind(LISTEN)
    log(f"Relay listening on {LISTEN[0]}:{LISTEN[1]}")
    links = {}   # client address -> [socket connected to the AC, last activity]
    owners = {}  # socket connected to the AC -> client address
    while True:
        ready, _, _ = select.select([lsock] + list(owners), [], [], 30)
        now = time.time()
        for s in ready:
            if s is lsock:
                try:
                    data, client = lsock.recvfrom(65535)
                except OSError:
                    continue
                link = links.get(client)
                if link is None:
                    ip = read_target(target_file)
                    if ip is None:
                        log("No valid AC address in the target file yet; dropped a packet")
                        continue
                    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    up.connect((ip, AC_PORT))
                    link = links[client] = [up, now]
                    owners[up] = client
                    log(f"Linked {client[0]}:{client[1]} to the AC at {ip}:{AC_PORT}")
                link[1] = now
                try:
                    link[0].send(data)
                except OSError as e:
                    log(f"Sending to the AC failed: {e}")
            else:
                client = owners.get(s)
                try:
                    data = s.recv(65535)
                except OSError:
                    continue
                if client is not None:
                    try:
                        lsock.sendto(data, client)
                    except OSError:
                        pass
                    links[client][1] = now
        for client, (up, last) in list(links.items()):
            if now - last > IDLE:
                owners.pop(up, None)
                up.close()
                del links[client]


if __name__ == "__main__":
    main()
