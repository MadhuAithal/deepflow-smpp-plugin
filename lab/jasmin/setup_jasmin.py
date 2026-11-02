#!/usr/bin/env python3
"""
setup_jasmin.py: configures Jasmin through its jCli console (telnet 127.0.0.1:8990).

Creates
  - user acme / secret        what send_otps.py binds as (on Jasmin's port 2775)
  - connector "operator"      Jasmin's own bind to fake_smsc.py on 127.0.0.1:2776,
                              as a transceiver (so receipts come back) and unthrottled
  - a default route           every message goes to "operator"
then saves the configuration. Safe to re-run: existing items are reported, not duplicated.

Run:  python3 setup_jasmin.py
"""
import re
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 8990
JCLI_USER, JCLI_PASS = "jcliadmin", "jclipwd"   # Jasmin's defaults; lab only

STEPS = [
    ("group",     ["group -a", "gid acme", "ok"]),
    ("user",      ["user -a", "username acme", "password secret", "gid acme", "uid acme", "ok"]),
    ("connector", ["smppccm -a", "cid operator", "host 127.0.0.1", "port 2776",
                   "username jasmin", "password jasmin", "bind transceiver",
                   "submit_throughput 0", "ok"]),
    ("start",     ["smppccm -1 operator"]),
    ("route",     ["mtrouter -a", "type defaultroute", "connector smppc(operator)", "rate 0.00", "ok"]),
    ("save",      ["persist"]),
]
PROBLEM = re.compile(r"error|unknown|already exist|invalid|cannot", re.I)
PROMPT = r"(jcli : |> )\s*$"


class JCli:
    def __init__(self):
        for i in range(40):
            try:
                self.s = socket.create_connection((HOST, PORT), timeout=5)
                break
            except OSError:
                if i == 0:
                    print("waiting for jCli on 127.0.0.1:8990 ...")
                time.sleep(3)
        else:
            sys.exit("jCli not reachable. Is Jasmin up?  docker compose ps / docker compose logs jasmin")
        self.buf = b""

    def read_until(self, pattern, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            # drop telnet negotiation bytes (IAC ...)
            text = re.sub(rb"\xff[\xfb-\xfe].|\xff[^\xfb-\xfe]", b"", self.buf).decode(errors="replace")
            if re.search(pattern, text):
                self.buf = b""
                return text
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            self.buf += chunk
        sys.exit(f"jCli did not answer as expected (waiting for {pattern!r}). Last output:\n{self.buf!r}")

    def send(self, line, expect=PROMPT):
        self.s.sendall(line.encode() + b"\r\n")
        return self.read_until(expect)


def main():
    j = JCli()
    j.read_until(r"Authentication required")   # no "Username:" prompt is shown; it just waits
    j.send(JCLI_USER, r"Password:\s*$")
    j.send(JCLI_PASS)
    for name, lines in STEPS:
        out = ""
        for line in lines:
            out = j.send(line)
            if PROBLEM.search(out):
                break
        if out.rstrip().endswith(">"):      # still inside an "add" dialogue after a problem
            j.send("ko")
        msgs = [l.strip() for l in out.splitlines()
                if l.strip() and not l.strip().startswith(("jcli :", ">"))]
        print(f"{name:<10} {msgs[-1] if msgs else 'done'}")
    time.sleep(3)   # give the connector a moment to bind
    print()
    print(j.send("smppccm --list").replace("jcli : ", "").strip())
    j.s.sendall(b"quit\r\n")


if __name__ == "__main__":
    main()
