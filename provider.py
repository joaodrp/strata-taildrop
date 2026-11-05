#!/usr/bin/python3
"""Strata file provider: sends the selected files to a tailnet device with Taildrop.

Speaks Strata's external provider protocol (newline-delimited JSON on
stdin/stdout). Each device that can receive appears under Send to -> Taildrop.
The transfer runs detached so a large send never blocks Strata's menus; its
result arrives as a desktop notification.

https://github.com/joaodrp/strata-taildrop
"""
from collections import OrderedDict
import ipaddress
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time

__version__ = "0.0.0"  # x-release-please-version

MAX_FRAME = 1024 * 1024
MAX_ACTIONS = 64
MAX_TICKETS = 256
PEERS_SECONDS = 5
TICKET_SECONDS = 120
# ipnstate.TaildropTargetAvailable; other values (offline, owned by another
# user, unsupported OS) cannot receive.
TARGET_AVAILABLE = 1


def tool(name):
    return shutil.which(name, path="/usr/bin:/bin")


def target(ip):
    """`tailscale file cp` wants IPv6 literals bracketed."""
    return f"[{ip}]:" if ipaddress.ip_address(ip).version == 6 else f"{ip}:"


def label_ok(name):
    return 0 < len(name.encode("utf-8")) <= 128 and name.isprintable()


def tailnet_peers():
    """Online peers that accept Taildrop. Named by MagicDNS short name, since
    some peers report a HostName of "localhost"; identified by stable node ID."""
    binary = tool("tailscale")
    if binary is None:
        return []
    try:
        out = subprocess.run([binary, "status", "--json"], capture_output=True,
                             timeout=3, check=True, cwd="/").stdout
        status = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    peers = []
    for peer in (status.get("Peer") or {}).values():
        name = (peer.get("DNSName") or "").split(".")[0]
        ips = sorted(peer.get("TailscaleIPs") or [], key=lambda ip: ":" in ip)  # IPv4 first
        if peer.get("TaildropTarget") == TARGET_AVAILABLE and peer.get("ID") and ips and label_ok(name):
            peers.append({"id": peer["ID"], "name": name, "ip": ips[0]})
    return sorted(peers, key=lambda p: p["name"])


def identity(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def identities(paths):
    """Regular files only: Taildrop cannot send folders or special files."""
    result = []
    for path in paths:
        s = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(s.st_mode):
            raise ValueError("not a regular file")
        result.append(identity(s))
    return tuple(result)


def open_verified(paths, expected):
    """Open each file without following a final symlink and check it is still
    the file the menu was built for. The transfer then reads these descriptors,
    so a file swapped in at that path afterwards is never sent. O_NONBLOCK keeps
    a FIFO swapped in from blocking the open; the type check then rejects it."""
    fds = []
    try:
        for path, want in zip(paths, expected, strict=True):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            fds.append(fd)
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or identity(s) != want:
                raise ValueError("changed")
        return fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def send(dest, name, pairs):
    """Detached sender: stream each verified descriptor, then notify."""
    binary = tool("tailscale")
    error = "" if binary else "tailscale is not installed"
    for fd, filename in pairs if binary else ():
        try:
            done = subprocess.run([binary, "file", "cp", "--name", filename, "-", dest],
                                  stdin=fd, capture_output=True, text=True, cwd="/")
        except OSError as exc:
            error = str(exc)
            break
        if done.returncode != 0:
            error = (done.stderr or done.stdout).strip() or f"exit status {done.returncode}"
            break
    # Omarchy's notifier matches its screenshot and update notices; notify-send covers other desktops.
    notify = tool("omarchy-notification-send") or tool("notify-send")
    if notify:
        count = f"{len(pairs)} file(s) to {name}"
        args = ["Taildrop failed", f"{count}: {error}"] if error else ["Sent via Taildrop", count]
        subprocess.run([notify, *args], cwd="/")
    return 1 if error else 0


def spawn(dest, name, fds, paths):
    argv = [sys.executable, "-I", os.path.abspath(__file__), "--send", dest, name]
    for fd, path in zip(fds, paths):
        argv += [str(fd), os.path.basename(path)]
    # A new session keeps the transfer alive when Strata stops this provider;
    # a waiter thread reaps it so no zombie lingers.
    child = subprocess.Popen(argv, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=fds)
    threading.Thread(target=child.wait, daemon=True).start()


class Provider:
    def __init__(self, peers=tailnet_peers, spawn=spawn, clock=time.monotonic):
        self.peers_source = peers
        self.spawn = spawn
        self.clock = clock
        self.cache = (None, [])
        self.tickets = OrderedDict()

    def peers(self):
        stamp, peers = self.cache
        now = self.clock()
        if stamp is None or now - stamp > PEERS_SECONDS:
            peers = self.peers_source()
            self.cache = (now, peers)
        return peers

    def ticket(self, paths, found, peer, action):
        """Reuse an unexpired ticket for the same selection and peer, so an
        unchanged menu keeps its context; the cache is bounded."""
        now = self.clock()
        for token, t in list(self.tickets.items()):
            if t["expires"] < now:
                del self.tickets[token]
            elif (t["paths"], t["found"], t["peer"], t["action"]) == (paths, found, peer, action):
                self.tickets.move_to_end(token)
                return token
        token = secrets.token_hex(24)
        self.tickets[token] = {"paths": paths, "found": found, "peer": peer,
                               "action": action, "expires": now + TICKET_SECONDS}
        while len(self.tickets) > MAX_TICKETS:
            self.tickets.popitem(last=False)
        return token

    def menu(self, paths, background):
        if background:
            return []
        try:
            found = identities(paths)
        except (OSError, ValueError):
            return []
        actions = []
        for index, peer in enumerate(self.peers()[:MAX_ACTIONS]):
            action = f"peer-{index}"
            token = self.ticket(tuple(paths), found, dict(peer), action)
            actions.append({"id": action, "label": peer["name"], "placement": "send-to", "context": token})
        return actions

    def activate(self, request):
        paths = request["paths"]
        total = len(paths)

        def result(status, message):
            reply = {"message": message, "outcome": {
                "status": status, "accepted": total if status == "accepted" else 0, "total": total}}
            return {**reply, "feedback": "notice"} if status == "accepted" else reply

        context = request.get("context")
        # Consume the ticket first so a lost reply cannot send twice.
        t = self.tickets.pop(context, None) if isinstance(context, str) else None
        if (t is None or t["paths"] != tuple(paths) or t["action"] != request.get("action")
                or request["background"] or t["expires"] < self.clock()):
            return result("rejected", "This menu expired. Reopen the right-click menu.")
        peer = t["peer"]
        # The same node, at the same address, must still be able to receive.
        if not any(p["id"] == peer["id"] and p["ip"] == peer["ip"] for p in self.peers_source()):
            return result("rejected", f"{peer['name']} can no longer receive files. Reopen the right-click menu.")
        try:
            fds = open_verified(paths, t["found"])
        except (OSError, ValueError):
            return result("rejected", "The selected files changed. Reopen the right-click menu.")
        try:
            self.spawn(target(peer["ip"]), peer["name"], fds, list(paths))
        except OSError:
            return result("rejected", "Taildrop could not be started.")
        finally:
            for fd in fds:
                os.close(fd)
        return result("accepted", f"Sending {total} file(s) to {peer['name']}.")

    def handle(self, request):
        rid = request.get("id")
        reply = {"version": 1, "id": rid if type(rid) is int and rid >= 0 else 0}
        paths = request.get("paths")
        try:
            valid = (type(request.get("version")) is int and request["version"] == 1
                     and type(rid) is int and rid >= 0
                     and type(request.get("background")) is bool and isinstance(paths, list)
                     and 1 <= len(paths) <= 200 and (len(paths) == 1 or not request["background"])
                     and all(isinstance(p, str) and p.startswith("/") and "\0" not in p
                             and len(p.encode("utf-8")) <= 16384 for p in paths))
        except UnicodeError:
            valid = False
        if not valid:
            return {**reply, "error": "invalid-request", "message": "Invalid provider request."}
        method = request.get("method")
        if method == "query":
            return {**reply, "decorations": []}
        if method == "menu":
            return {**reply, "actions": self.menu(paths, request["background"])}
        if method == "activate":
            return {**reply, **self.activate(request)}
        return {**reply, "error": "unsupported-method", "message": "Unsupported provider method."}


def main():
    provider = Provider()
    stdin, stdout = sys.stdin.buffer, sys.stdout
    while True:
        frame = stdin.readline(MAX_FRAME + 1)
        if not frame.endswith(b"\n"):
            return
        try:
            request = json.loads(frame)
            reply = provider.handle(request) if isinstance(request, dict) else None
        except ValueError:
            reply = None
        if reply is None:
            reply = {"version": 1, "id": 0, "error": "invalid-request", "message": "Invalid provider request."}
        stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
        stdout.flush()


def cli(argv):
    """`--send <target> <name> (<fd> <filename>)...` is the detached sender;
    anything else is the protocol loop Strata talks to."""
    if argv[:1] == ["--send"] and len(argv) >= 5 and len(argv) % 2 == 1:
        return send(argv[1], argv[2], [(int(fd), name) for fd, name in zip(argv[3::2], argv[4::2])])
    main()
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
