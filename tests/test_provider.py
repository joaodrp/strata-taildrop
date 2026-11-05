"""Tests for provider.py, standard library only. Run from the repository root:

    python3 -I -m unittest discover -s tests
"""
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    "provider", pathlib.Path(__file__).resolve().parent.parent / "provider.py")
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


def fake_tool(root, name, script):
    """Write an executable stand-in for a tool and return its path."""
    path = os.path.join(root, name)
    pathlib.Path(path).write_text("#!/bin/sh\n" + script)
    os.chmod(path, 0o700)
    return path


def request(method, paths, **extra):
    return {"version": 1, "id": 1, "method": method, "paths": paths, "background": False, **extra}


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.a = os.path.join(self.root.name, "a b.txt")
        self.b = os.path.join(self.root.name, "-c.txt")
        for path in (self.a, self.b):
            pathlib.Path(path).write_text(path)
        self.live = [{"id": "n1", "name": "phone", "ip": "100.64.0.1"},
                     {"id": "n2", "name": "tablet", "ip": "fd7a::2"}]
        self.sent = []

        def spawn(dest, name, fds, paths):
            self.sent.append((dest, name, [os.pread(fd, 4096, 0).decode() for fd in fds]))

        self.p = provider.Provider(peers=lambda: [dict(x) for x in self.live], spawn=spawn)

    def menu(self, paths):
        return self.p.handle(request("menu", paths))["actions"]

    def activate(self, paths, action):
        return self.p.handle(request("activate", paths, action=action["id"], context=action["context"]))

    def test_menu_lists_peers_under_send_to(self):
        actions = self.menu([self.a, self.b])
        self.assertEqual([a["label"] for a in actions], ["phone", "tablet"])
        self.assertTrue(all(a["placement"] == "send-to" for a in actions))

    def test_unchanged_menu_reuses_tickets(self):
        first = [a["context"] for a in self.menu([self.a])]
        self.assertEqual([a["context"] for a in self.menu([self.a])], first)

    def test_folders_and_background_menus_get_nothing(self):
        self.assertEqual(self.menu([self.root.name]), [])
        self.assertEqual(self.p.handle({**request("menu", [self.a]), "background": True})["actions"], [])

    def test_activation_sends_the_verified_contents(self):
        tablet = self.menu([self.a, self.b])[1]
        reply = self.activate([self.a, self.b], tablet)
        self.assertEqual(reply["outcome"], {"status": "accepted", "accepted": 2, "total": 2})
        self.assertEqual(reply["feedback"], "notice")
        self.assertEqual(self.sent, [("[fd7a::2]:", "tablet", [self.a, self.b])])

    def test_ticket_is_single_use(self):
        phone = self.menu([self.a])[0]
        self.activate([self.a], phone)
        self.assertEqual(self.activate([self.a], phone)["outcome"]["status"], "rejected")
        self.assertEqual(len(self.sent), 1)

    def test_changed_file_is_refused(self):
        phone = self.menu([self.a])[0]
        pathlib.Path(self.a).write_text("changed")
        self.assertEqual(self.activate([self.a], phone)["outcome"]["status"], "rejected")
        self.assertEqual(self.sent, [])

    def test_wrong_action_is_refused(self):
        phone = self.menu([self.a])[0]
        reply = self.p.handle(request("activate", [self.a], action="peer-9", context=phone["context"]))
        self.assertEqual(reply["outcome"]["status"], "rejected")

    def test_readdressed_peer_is_refused(self):
        phone = self.menu([self.a])[0]
        self.live[0]["ip"] = "100.64.0.9"
        self.assertEqual(self.activate([self.a], phone)["outcome"]["status"], "rejected")
        self.assertEqual(self.sent, [])

    def test_invalid_requests(self):
        self.assertEqual(self.p.handle({**request("menu", [self.a]), "id": -1})["error"], "invalid-request")
        self.assertEqual(self.p.handle({**request("menu", [self.a]), "version": True})["error"], "invalid-request")
        self.assertEqual(self.p.handle(request("menu", ["relative"]))["error"], "invalid-request")
        self.assertEqual(self.p.handle(request("rename", [self.a]))["error"], "unsupported-method")
        reply = self.p.handle(request("activate", [self.a], action="x", context=["list"]))
        self.assertEqual(reply["outcome"]["status"], "rejected")

    def test_target_brackets_ipv6(self):
        self.assertEqual(provider.target("100.64.0.1"), "100.64.0.1:")
        self.assertEqual(provider.target("fd7a::1"), "[fd7a::1]:")

    def test_send_streams_each_descriptor(self):
        log = os.path.join(self.root.name, "log")
        fake = os.path.join(self.root.name, "tailscale")
        pathlib.Path(fake).write_text(f'#!/bin/sh\nprintf "%s|" "$@" >>"{log}"; cat >>"{log}"; echo >>"{log}"\n')
        os.chmod(fake, 0o700)
        real = provider.tool
        provider.tool = lambda name: fake if name == "tailscale" else None
        self.addCleanup(setattr, provider, "tool", real)
        fds = provider.open_verified([self.b], provider.identities([self.b]))
        self.addCleanup(os.close, fds[0])
        self.assertEqual(provider.send("100.64.0.1:", "phone", [(fds[0], "-c.txt")]), 0)
        self.assertEqual(pathlib.Path(log).read_text(), f"file|cp|--name|-c.txt|-|100.64.0.1:|{self.b}\n")

    def use_tools(self, **tools):
        real = provider.tool
        provider.tool = lambda name: tools.get(name)
        self.addCleanup(setattr, provider, "tool", real)

    def test_query_returns_no_decorations(self):
        self.assertEqual(self.p.handle(request("query", [self.a]))["decorations"], [])

    def test_symlink_selection_gets_nothing(self):
        link = os.path.join(self.root.name, "link.txt")
        os.symlink(self.a, link)
        self.assertEqual(self.menu([link]), [])

    def test_non_utf8_path_is_invalid(self):
        self.assertEqual(self.p.handle(request("menu", ["/\ud800"]))["error"], "invalid-request")

    def test_expired_ticket_is_refused_and_dropped(self):
        now = [0.0]
        p = provider.Provider(peers=lambda: [dict(x) for x in self.live], spawn=lambda *a: None,
                              clock=lambda: now[0])
        phone = p.handle(request("menu", [self.a]))["actions"][0]
        now[0] += provider.TICKET_SECONDS + 1
        p.handle(request("menu", [self.b]))  # prunes the expired ticket
        reply = p.handle(request("activate", [self.a], action=phone["id"], context=phone["context"]))
        self.assertEqual(reply["outcome"]["status"], "rejected")

    def test_ticket_cache_is_bounded(self):
        for i in range(provider.MAX_TICKETS):
            path = os.path.join(self.root.name, f"f{i}")
            pathlib.Path(path).write_text("x")
            self.menu([path])
        self.assertLessEqual(len(self.p.tickets), provider.MAX_TICKETS)

    def test_menu_is_capped(self):
        self.live[:] = [{"id": f"n{i}", "name": f"d{i:03}", "ip": f"100.64.1.{i}"} for i in range(100)]
        self.assertEqual(len(self.menu([self.a])), provider.MAX_ACTIONS)

    def test_spawn_failure_is_reported(self):
        def broken(*args):
            raise OSError("no fork")
        p = provider.Provider(peers=lambda: [dict(x) for x in self.live], spawn=broken)
        phone = p.handle(request("menu", [self.a]))["actions"][0]
        reply = p.handle(request("activate", [self.a], action=phone["id"], context=phone["context"]))
        self.assertEqual(reply["message"], "Taildrop could not be started.")

    def test_peers_come_from_tailscale_status(self):
        status = {"Peer": {
            "a": {"ID": "n1", "DNSName": "phone.tail.ts.net.", "TaildropTarget": 1,
                  "TailscaleIPs": ["fd7a::1", "100.64.0.1"]},
            "b": {"ID": "n2", "DNSName": "server.tail.ts.net.", "TaildropTarget": 9,
                  "TailscaleIPs": ["100.64.0.2"]},
            "c": {"ID": "n3", "DNSName": "v6only.tail.ts.net.", "TaildropTarget": 1,
                  "TailscaleIPs": ["fd7a::3"]},
            "d": {"DNSName": "noid.tail.ts.net.", "TaildropTarget": 1, "TailscaleIPs": ["100.64.0.4"]},
            "e": {"ID": "n5", "DNSName": "x" * 129 + ".ts.net.", "TaildropTarget": 1,
                  "TailscaleIPs": ["100.64.0.5"]},
        }}
        data = os.path.join(self.root.name, "status.json")
        pathlib.Path(data).write_text(json.dumps(status))
        self.use_tools(tailscale=fake_tool(self.root.name, "tailscale", f'cat "{data}"\n'))
        self.assertEqual(provider.tailnet_peers(), [
            {"id": "n1", "name": "phone", "ip": "100.64.0.1"},
            {"id": "n3", "name": "v6only", "ip": "fd7a::3"},
        ])

    def test_peers_when_tailscale_is_missing_or_failing(self):
        self.use_tools()
        self.assertEqual(provider.tailnet_peers(), [])
        self.use_tools(tailscale=fake_tool(self.root.name, "tailscale", "exit 1\n"))
        self.assertEqual(provider.tailnet_peers(), [])
        self.use_tools(tailscale=fake_tool(self.root.name, "tailscale", "echo not json\n"))
        self.assertEqual(provider.tailnet_peers(), [])

    def test_send_notifies_success_and_failure(self):
        log = os.path.join(self.root.name, "notify.log")
        notify = fake_tool(self.root.name, "notify", f'printf "%s|" "$@" >>"{log}"; echo >>"{log}"\n')
        fds = provider.open_verified([self.a], provider.identities([self.a]))
        self.addCleanup(os.close, fds[0])

        self.use_tools(tailscale=fake_tool(self.root.name, "ok", "cat >/dev/null\n"),
                       **{"omarchy-notification-send": notify})
        self.assertEqual(provider.send("100.64.0.1:", "phone", [(fds[0], "a b.txt")]), 0)

        self.use_tools(tailscale=fake_tool(self.root.name, "bad", "echo 'no route' >&2; exit 1\n"),
                       **{"omarchy-notification-send": notify})
        self.assertEqual(provider.send("100.64.0.1:", "phone", [(fds[0], "a b.txt")]), 1)

        unrunnable = os.path.join(self.root.name, "unrunnable")
        pathlib.Path(unrunnable).write_text("")  # not executable: running it raises OSError
        self.use_tools(tailscale=unrunnable, **{"omarchy-notification-send": notify})
        self.assertEqual(provider.send("100.64.0.1:", "phone", [(fds[0], "a b.txt")]), 1)

        self.use_tools(**{"omarchy-notification-send": notify})  # tailscale missing
        self.assertEqual(provider.send("100.64.0.1:", "phone", [(fds[0], "a b.txt")]), 1)

        self.use_tools(tailscale=fake_tool(self.root.name, "ok2", "cat >/dev/null\n"), **{"notify-send": notify})
        self.assertEqual(provider.send("100.64.0.1:", "tablet", [(fds[0], "a b.txt")]), 0)

        self.assertEqual(pathlib.Path(log).read_text().splitlines(), [
            "Sent via Taildrop|1 file(s) to phone|",
            "Taildrop failed|1 file(s) to phone: no route|",
            f"Taildrop failed|1 file(s) to phone: [Errno 13] Permission denied: '{unrunnable}'|",
            "Taildrop failed|1 file(s) to phone: tailscale is not installed|",
            "Sent via Taildrop|1 file(s) to tablet|",
        ])

    def test_spawn_detaches_and_reaps_the_sender(self):
        calls, reaped = [], threading.Event()

        class Child:
            def wait(self):
                reaped.set()

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return Child()

        real = provider.subprocess.Popen
        provider.subprocess.Popen = popen
        self.addCleanup(setattr, provider.subprocess, "Popen", real)
        provider.spawn("100.64.0.1:", "phone", [7, 8], ["/x/a b.txt", "/y/-c.txt"])
        argv, kwargs = calls[0]
        self.assertEqual(argv[1:2] + argv[3:], ["-I", "--send", "100.64.0.1:", "phone", "7", "a b.txt", "8", "-c.txt"])
        self.assertEqual(kwargs["pass_fds"], [7, 8])
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(reaped.wait(5), "a waiter thread reaps the sender")

    def test_spawn_hands_the_open_files_to_a_real_child(self):
        # Run spawn for real, with the sender swapped for a stub that copies
        # what arrives on each descriptor, to prove the handoff itself.
        out = os.path.join(self.root.name, "received")
        stub = os.path.join(self.root.name, "stub.py")
        pathlib.Path(stub).write_text(
            "import os, sys\n"
            "a = sys.argv[1:]\n"
            "with open(%r, 'w') as f:\n"
            "    f.write(' '.join(a[:3]) + '\\n')\n"
            "    for fd, name in zip(a[3::2], a[4::2]):\n"
            "        f.write(name + '=' + os.read(int(fd), 4096).decode() + '\\n')\n" % out)
        real = provider.__file__
        provider.__file__ = stub
        self.addCleanup(setattr, provider, "__file__", real)
        fds = provider.open_verified([self.a, self.b], provider.identities([self.a, self.b]))
        try:
            provider.spawn("100.64.0.1:", "phone", fds, [self.a, self.b])
        finally:
            for fd in fds:
                os.close(fd)
        for _ in range(500):
            if os.path.exists(out) and pathlib.Path(out).read_text().count("\n") == 3:
                break
            threading.Event().wait(0.01)
        self.assertEqual(pathlib.Path(out).read_text(),
                         f"--send 100.64.0.1: phone\na b.txt={self.a}\n-c.txt={self.b}\n")

    def test_send_mode_runs_the_sender(self):
        log = os.path.join(self.root.name, "cp.log")
        self.use_tools(tailscale=fake_tool(self.root.name, "tailscale",
                                           f'printf "%s|" "$@" >>"{log}"; cat >>"{log}"; echo >>"{log}"\n'))
        fds = provider.open_verified([self.a], provider.identities([self.a]))
        self.addCleanup(os.close, fds[0])
        self.assertEqual(provider.cli(["--send", "100.64.0.1:", "phone", str(fds[0]), "a b.txt"]), 0)
        self.assertEqual(pathlib.Path(log).read_text(), f"file|cp|--name|a b.txt|-|100.64.0.1:|{self.a}\n")

    def test_path_swapped_after_the_check_is_not_sent(self):
        fds = provider.open_verified([self.a], provider.identities([self.a]))
        self.addCleanup(os.close, fds[0])
        swap = os.path.join(self.root.name, "swap")
        pathlib.Path(swap).write_text("swapped in")
        os.replace(swap, self.a)
        self.assertEqual(os.pread(fds[0], 4096, 0).decode(), self.a, "the descriptor still reads the checked file")

    def test_fifo_swapped_in_is_refused_without_blocking(self):
        phone = self.menu([self.a])[0]
        os.unlink(self.a)
        os.mkfifo(self.a)
        done = threading.Event()
        result = {}

        def run():
            result["reply"] = self.activate([self.a], phone)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(5), "opening a FIFO must not block")
        self.assertEqual(result["reply"]["outcome"]["status"], "rejected")
        self.assertEqual(self.sent, [])

    def test_descriptors_are_closed_when_a_check_fails(self):
        before = set(os.listdir("/proc/self/fd"))
        expected = provider.identities([self.a, self.b])
        pathlib.Path(self.b).write_text("changed")
        with self.assertRaises(ValueError):
            provider.open_verified([self.a, self.b], expected)
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_send_stops_at_the_first_failed_file(self):
        log = os.path.join(self.root.name, "names")
        # Fails on the second file only.
        script = f'echo "$4" >>"{log}"; cat >/dev/null; [ "$4" = "-c.txt" ] && {{ echo "disk full" >&2; exit 1; }}; exit 0\n'
        notify_log = os.path.join(self.root.name, "notify.log")
        notify = fake_tool(self.root.name, "notify", f'printf "%s|" "$@" >>"{notify_log}"; echo >>"{notify_log}"\n')
        self.use_tools(tailscale=fake_tool(self.root.name, "tailscale", script),
                       **{"omarchy-notification-send": notify})
        fds = provider.open_verified([self.a, self.b, self.a], provider.identities([self.a, self.b, self.a]))
        for fd in fds:
            self.addCleanup(os.close, fd)
        pairs = list(zip(fds, ["a b.txt", "-c.txt", "again.txt"]))
        self.assertEqual(provider.send("100.64.0.1:", "phone", pairs), 1)
        self.assertEqual(pathlib.Path(log).read_text().splitlines(), ["a b.txt", "-c.txt"], "the third file is not sent")
        self.assertEqual(pathlib.Path(notify_log).read_text(), "Taildrop failed|3 file(s) to phone: disk full|\n")

    def test_main_speaks_newline_delimited_json(self):
        frames = b"".join([
            json.dumps(request("query", [self.a])).encode() + b"\n",
            b"not json\n",
            b"[1, 2]\n",
            b"truncated",  # no newline: the loop ends
        ])
        out = io.StringIO()
        real_in, real_out = provider.sys.stdin, provider.sys.stdout
        provider.sys.stdin = io.TextIOWrapper(io.BytesIO(frames))
        provider.sys.stdout = out
        self.addCleanup(setattr, provider.sys, "stdin", real_in)
        self.addCleanup(setattr, provider.sys, "stdout", real_out)
        provider.main()
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0], {"version": 1, "id": 1, "decorations": []})
        self.assertEqual([r.get("error") for r in replies[1:]], ["invalid-request", "invalid-request"])

    def test_cli_without_send_runs_the_protocol_loop(self):
        real_in = provider.sys.stdin
        provider.sys.stdin = io.TextIOWrapper(io.BytesIO(b""))
        self.addCleanup(setattr, provider.sys, "stdin", real_in)
        self.assertEqual(provider.cli([]), 0)

    def test_tool_looks_only_in_system_directories(self):
        self.assertIn(provider.tool("sh"), ("/usr/bin/sh", "/bin/sh"))
        self.assertIsNone(provider.tool("definitely-not-a-real-tool"))

    def test_other_arguments_run_the_protocol_loop(self):
        # A malformed `--send` is not a send: the protocol loop runs, and ends at
        # once on empty stdin.
        done = subprocess.run([provider.sys.executable, "-I", provider.__file__, "--send", "x:"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        self.assertEqual((done.returncode, done.stdout), (0, b""))

if __name__ == "__main__":
    unittest.main()
