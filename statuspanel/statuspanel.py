#!/usr/bin/env python3
"""statuspanel - network infrastructure status on a PiDP-11 front panel.

Each configured check owns one LED. While a poll runs the LED is lit (at
least `min_on_ms`, longer if the poll is slow). A healthy result turns the
LED off; an unhealthy result makes it blink fast until the system recovers.

Everything is driven by one JSON file (config.json) which can be edited from
the built-in web page; changes are applied live without a restart. Status is
served as a web page / JSON API and published to Home Assistant via MQTT
discovery.
"""
import argparse
import copy
import hmac
import json
import logging
import mmap
import os
import random
import re
import socket
import sqlite3
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("statuspanel")
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.1"

# --------------------------------------------------------------------------
# Panel LED map  (row, column) in the PiDP-11 6x12 matrix, in display order
# --------------------------------------------------------------------------
LEDS = {}
for _b in range(21, 11, -1):
    LEDS["A%d" % _b] = (1, _b - 12)
for _b in range(11, -1, -1):
    LEDS["A%d" % _b] = (0, _b)
for _b in range(15, 11, -1):
    LEDS["D%d" % _b] = (4, _b - 12)
for _b in range(11, -1, -1):
    LEDS["D%d" % _b] = (3, _b)
LEDS.update({
    "PAR_ERR": (2, 11), "ADRS_ERR": (2, 10), "RUN": (2, 9), "PAUSE": (2, 8), "MASTER": (2, 7),
    "USER": (2, 6), "SUPER": (2, 5), "KERNEL": (2, 4), "DATA": (2, 3),
    "ADDR16": (2, 2), "ADDR18": (2, 1), "ADDR22": (2, 0),
    "PAR_HIGH": (4, 5), "PAR_LOW": (4, 4),
    "USER_D": (4, 6), "SUPER_D": (4, 7), "KERNEL_D": (4, 8), "CONS_PHY": (4, 9),
    "PROG_PHY": (5, 8), "KERNEL_I": (5, 7), "SUPER_I": (5, 6), "USER_I": (5, 5),
    "DATA_PATHS": (4, 10), "BUS_REG": (4, 11), "UADRS": (5, 10), "DISP_REG": (5, 11),
})

SHM_PATH = "/dev/shm/statuspanel"
SHM_MAGIC = 0x31504450
SHM_FMT = "<III6H3HxxI8I"
SHM_SIZE = struct.calcsize(SHM_FMT)  # 68
OFF_HEARTBEAT, OFF_LEDS, OFF_SWITCHES, OFF_SCAN = 8, 12, 24, 32

DEFAULT_SETTINGS = {
    "web_port": 80,
    "interval": 1.5,
    "jitter": 0.5,
    "timeout": 3,
    "fail_after": 3,
    "min_on_ms": 200,
    "blink_hz": 4,
    "run_led": "RUN",
    "alarm_led": "ADRS_ERR",
    "lamp_test_switch": [2, 0, 1],
    "poll_all_switch": [2, 7, 0],
    "database": "/var/lib/statuspanel/history.db",
    "history_days": 14,
    "history_sample_s": 60,
    "edit_password": "",
    "allow_command_checks_from_web": False,
}
DEFAULT_HA = {
    "enabled": False, "host": "homeassistant.local", "port": 1883, "username": "", "password": "",
    "discovery_prefix": "homeassistant", "device_name": "Infrastructure Status Panel",
}
# Settings that can be edited from the web page (others need a file edit + restart)
WEB_SETTINGS = ["interval", "jitter", "timeout", "fail_after", "min_on_ms", "blink_hz", "run_led", "alarm_led",
                "history_days", "history_sample_s"]

# Check type schema: shared by the validator and the web editor
COMMON_FIELDS = ["id", "name", "led", "type", "interval", "timeout", "fail_after", "location", "group"]
TYPES = {
    "ping": {"label": "Ping", "fields": [
        {"key": "host", "label": "Host / IP", "kind": "text", "required": True}]},
    "tcp": {"label": "TCP port open", "fields": [
        {"key": "host", "label": "Host / IP", "kind": "text", "required": True},
        {"key": "port", "label": "Port", "kind": "int", "required": True}]},
    "http": {"label": "HTTP(S) request", "fields": [
        {"key": "url", "label": "URL", "kind": "text", "required": True},
        {"key": "expect_status", "label": "Expected status", "kind": "text", "placeholder": "200-399"},
        {"key": "contains", "label": "Body must contain", "kind": "text"},
        {"key": "verify_tls", "label": "Verify TLS certificate", "kind": "bool", "default": True}]},
    "dns": {"label": "DNS lookup", "fields": [
        {"key": "query", "label": "Name to resolve", "kind": "text", "required": True},
        {"key": "server", "label": "DNS server (blank = system)", "kind": "text"}]},
    "json": {"label": "JSON field", "fields": [
        {"key": "url", "label": "URL", "kind": "text", "required": True},
        {"key": "field", "label": "Field (dotted path)", "kind": "text", "required": True},
        {"key": "equals", "label": "Healthy values (comma separated)", "kind": "list"},
        {"key": "increasing", "label": "Healthy while value keeps increasing", "kind": "bool"},
        {"key": "stale_s", "label": "...stuck for this many seconds = unhealthy", "kind": "float", "placeholder": "60"},
        {"key": "detail_field", "label": "Field to show in log", "kind": "text"}]},
    "agw": {"label": "AGWPE handshake (Direwolf)", "fields": [
        {"key": "host", "label": "Host / IP", "kind": "text", "required": True},
        {"key": "port", "label": "Port", "kind": "int", "placeholder": "8000"}]},
    "command": {"label": "Shell command (exit 0 = healthy)", "fields": [
        {"key": "command", "label": "Command", "kind": "text", "required": True}]},
}


def parse_led(name):
    name = str(name).upper()
    if name in LEDS:
        return LEDS[name]
    m = re.fullmatch(r"R(\d)C(\d{1,2})", name)
    if m and int(m.group(1)) < 6 and int(m.group(2)) < 12:
        return int(m.group(1)), int(m.group(2))
    raise ValueError("unknown LED %r" % name)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "check"


# --------------------------------------------------------------------------
# Configuration file
# --------------------------------------------------------------------------
def normalize_check(raw):
    """Validate one check dict and return a cleaned copy. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("check must be an object")
    d = {k: v for k, v in raw.items() if v not in (None, "") and not str(k).startswith("_")}
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("every check needs a name")
    d["name"] = name
    t = d.get("type")
    if t not in TYPES:
        raise ValueError("%s: unknown type %r" % (name, t))
    if "led" not in d:
        raise ValueError("%s: no LED assigned" % name)
    d["led"] = str(d["led"]).upper()
    parse_led(d["led"])
    d["id"] = slugify(d.get("id") or name)
    allowed = set(COMMON_FIELDS) | {f["key"] for f in TYPES[t]["fields"]}
    for k in list(d):
        if k not in allowed:
            del d[k]
    try:
        for k in ("interval", "timeout", "stale_s"):
            if k in d:
                d[k] = float(d[k])
                if d[k] <= 0:
                    raise ValueError("%s must be positive" % k)
        for k in ("fail_after", "port"):
            if k in d:
                d[k] = int(d[k])
    except (TypeError, ValueError) as e:
        raise ValueError("%s: %s" % (name, e))
    for f in TYPES[t]["fields"]:
        k = f["key"]
        if f.get("required") and k not in d:
            raise ValueError("%s: %s is required" % (name, f["label"]))
        if f["kind"] == "bool" and k in d:
            d[k] = d[k] if isinstance(d[k], bool) else str(d[k]).lower() in ("1", "true", "yes", "on")
        if f["kind"] == "list" and k in d and isinstance(d[k], str):
            d[k] = [x.strip() for x in d[k].split(",") if x.strip()]
    return d


def normalize_config(cfg):
    out = {"settings": dict(DEFAULT_SETTINGS), "homeassistant": dict(DEFAULT_HA), "checks": []}
    out["settings"].update(cfg.get("settings") or {})
    out["homeassistant"].update(cfg.get("homeassistant") or {})
    s = out["settings"]
    for k in ("run_led", "alarm_led"):
        if s.get(k):
            s[k] = str(s[k]).upper()
            parse_led(s[k])
        else:
            s[k] = ""
    used, ids = {}, set()
    for k in ("run_led", "alarm_led"):
        if s[k]:
            used[parse_led(s[k])] = "%s indicator" % k.split("_")[0].upper()
    for raw in cfg.get("checks") or []:
        c = normalize_check(raw)
        base, n = c["id"], 2
        while c["id"] in ids:
            c["id"] = "%s_%d" % (base, n)
            n += 1
        ids.add(c["id"])
        pos = parse_led(c["led"])
        if pos in used:
            raise ValueError("LED %s is used by both %r and %r" % (c["led"], used[pos], c["name"]))
        used[pos] = c["name"]
        out["checks"].append(c)
    return out


def load_config(path):
    with open(path) as f:
        return normalize_config(json.load(f))


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Shared memory link to ledmux
# --------------------------------------------------------------------------
class Panel:
    def __init__(self):
        self.mm = None
        try:
            fd = os.open(SHM_PATH, os.O_RDWR | os.O_CREAT, 0o666)
            if os.fstat(fd).st_size < SHM_SIZE:
                os.ftruncate(fd, SHM_SIZE)
            self.mm = mmap.mmap(fd, SHM_SIZE)
            os.close(fd)
            if struct.unpack_from("<I", self.mm, 0)[0] != SHM_MAGIC:
                self.mm[:] = b"\0" * SHM_SIZE
                struct.pack_into("<II", self.mm, 0, SHM_MAGIC, 1)
        except OSError as e:
            log.warning("panel shared memory unavailable (%s); running headless", e)
        self.hb = 0

    def write(self, rows):
        if not self.mm:
            return
        struct.pack_into("<6H", self.mm, OFF_LEDS, *rows)
        self.hb = (self.hb + 1) & 0xFFFFFFFF
        struct.pack_into("<I", self.mm, OFF_HEARTBEAT, self.hb)

    def switches(self):
        if not self.mm:
            return (0, 0xFFF, 0xFFE)
        return struct.unpack_from("<3H", self.mm, OFF_SWITCHES)

    def scan_count(self):
        return struct.unpack_from("<I", self.mm, OFF_SCAN)[0] if self.mm else 0


# --------------------------------------------------------------------------
# Probes. Each returns (ok, latency_ms, detail)
# --------------------------------------------------------------------------
def _elapsed(t0):
    return (time.monotonic() - t0) * 1000


def probe_ping(c):
    t0 = time.monotonic()
    r = subprocess.run(["ping", "-n", "-c", "1", "-W", str(max(1, int(c["timeout"]))), c["host"]],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=c["timeout"] + 2)
    ms = _elapsed(t0)
    m = re.search(r"time[=<]([\d.]+) ms", r.stdout)
    if m:
        ms = float(m.group(1))
    if r.returncode == 0:
        return True, ms, "reply"
    lines = r.stdout.strip().splitlines()
    return False, ms, lines[-1] if lines else "no reply"


def probe_tcp(c):
    t0 = time.monotonic()
    try:
        with socket.create_connection((c["host"], int(c["port"])), timeout=c["timeout"]):
            pass
        return True, _elapsed(t0), "port %s open" % c["port"]
    except OSError as e:
        return False, _elapsed(t0), str(e) or e.__class__.__name__


def _status_range(expect):
    if expect is None or expect == "":
        return 200, 399
    if isinstance(expect, int):
        return expect, expect
    if isinstance(expect, list):
        return int(expect[0]), int(expect[-1])
    parts = re.split(r"\s*[-,]\s*", str(expect).strip())
    return int(parts[0]), int(parts[-1])


def _ssl_ctx(c):
    if c.get("verify_tls", True):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def probe_http(c):
    url = c["url"]
    req = urllib.request.Request(url, headers={"User-Agent": "pidp11-statuspanel/%s" % VERSION})
    lo, hi = _status_range(c.get("expect_status"))
    t0 = time.monotonic()
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=c["timeout"],
                                          context=_ssl_ctx(c) if url.startswith("https") else None)
        except urllib.error.HTTPError as e:
            resp = e
        code = resp.getcode()
        body = resp.read(262144).decode("utf-8", "replace") if c.get("contains") else ""
        ms = _elapsed(t0)
        if not lo <= code <= hi:
            return False, ms, "HTTP %d" % code
        if c.get("contains") and c["contains"] not in body:
            return False, ms, "HTTP %d, text %r not found" % (code, c["contains"])
        return True, ms, "HTTP %d" % code
    except Exception as e:  # noqa: BLE001 - any failure is "unhealthy"
        return False, _elapsed(t0), str(getattr(e, "reason", e)) or e.__class__.__name__


def _dns_query(server, name, timeout, port=53):
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    q += struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, (server, port))
        while True:
            data, _ = s.recvfrom(2048)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid:
                break
    flags, _qd, an = struct.unpack(">HHH", data[2:8])
    return flags & 0xF, an


def probe_dns(c):
    name = c["query"]
    t0 = time.monotonic()
    try:
        if c.get("server"):
            rcode, an = _dns_query(c["server"], name, c["timeout"])
            ms = _elapsed(t0)
            if rcode != 0:
                return False, ms, "rcode %d for %s" % (rcode, name)
            if an == 0:
                return False, ms, "no answer for %s" % name
            return True, ms, "%s resolved (%d answers)" % (name, an)
        addr = socket.gethostbyname(name)
        return True, _elapsed(t0), "%s -> %s" % (name, addr)
    except Exception as e:  # noqa: BLE001
        return False, _elapsed(t0), str(e) or e.__class__.__name__


def probe_command(c):
    t0 = time.monotonic()
    try:
        r = subprocess.run(c["command"], shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=c["timeout"])
        out = (r.stdout.strip().splitlines() or [""])[-1][:200]
        return r.returncode == 0, _elapsed(t0), out or "exit %d" % r.returncode
    except subprocess.TimeoutExpired:
        return False, _elapsed(t0), "timed out"


def _json_field(obj, path):
    for part in str(path).split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def probe_json(c, state):
    """Fetch JSON and test one field: `equals` list, or `increasing` within stale_s."""
    t0 = time.monotonic()
    url = c["url"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pidp11-statuspanel/%s" % VERSION})
        with urllib.request.urlopen(req, timeout=c["timeout"],
                                    context=_ssl_ctx(c) if url.startswith("https") else None) as r:
            data = json.loads(r.read(4 * 1024 * 1024))
        ms = _elapsed(t0)
        val = _json_field(data, c["field"])
    except Exception as e:  # noqa: BLE001
        return False, _elapsed(t0), str(getattr(e, "reason", e)) or e.__class__.__name__
    extra = ""
    if c.get("detail_field"):
        try:
            extra = " - %s" % _json_field(data, c["detail_field"])
        except Exception:  # noqa: BLE001
            pass
    if c.get("increasing"):
        now = time.monotonic()
        try:
            num = float(val)
        except (TypeError, ValueError):
            return False, ms, "%s=%r is not a number" % (c["field"], val)
        if "last" not in state or num > state["last"]:
            rate = ""
            if "last" in state and now > state["t"]:
                rate = ", %.0f/s" % ((num - state["last"]) / (now - state["t"]))
            state.update(last=num, t=now, changed=now)
            return True, ms, "%s=%s%s" % (c["field"], val, rate)
        state.update(last=num, t=now)
        idle = now - state["changed"]
        if idle > float(c.get("stale_s", 60)):
            return False, ms, "%s stuck at %s for %ds" % (c["field"], val, idle)
        return True, ms, "%s=%s" % (c["field"], val)
    want = c.get("equals")
    if not want:
        return bool(val), ms, "%s=%s%s" % (c["field"], val, extra)
    wants = [str(w) for w in (want if isinstance(want, list) else [want])]
    sval = str(val).lower() if isinstance(val, bool) else str(val)
    return sval in wants, ms, "%s=%s%s" % (c["field"], val, extra)


def probe_agw(c):
    """AGWPE handshake (Direwolf, soundmodem): send version request 'R', expect 'R' reply."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((c["host"], int(c.get("port", 8000))), timeout=c["timeout"]) as s:
            s.sendall(struct.pack("<B3xB3xI10s10sII", 0, ord("R"), 0, b"", b"", 0, 0))
            data = b""
            while len(data) < 44:
                chunk = s.recv(64)
                if not chunk:
                    break
                data += chunk
        ms = _elapsed(t0)
        if len(data) >= 36 and data[4:5] == b"R":
            ver = struct.unpack_from("<HxxHxx", data, 36) if len(data) >= 44 else ("?", "?")
            return True, ms, "AGW version %s.%s" % ver
        return False, ms, "unexpected AGW reply (%d bytes)" % len(data)
    except OSError as e:
        return False, _elapsed(t0), str(e) or e.__class__.__name__


def run_probe(cfg, state):
    t = cfg["type"]
    if t == "json":
        return probe_json(cfg, state)
    return {"ping": probe_ping, "tcp": probe_tcp, "http": probe_http, "dns": probe_dns,
            "command": probe_command, "agw": probe_agw}[t](cfg)


def describe_target(c):
    t = c["type"]
    if t in ("tcp", "agw"):
        return "%s:%s" % (c["host"], c.get("port", 8000))
    if t == "dns":
        return "%s @%s" % (c["query"], c.get("server", "system resolver"))
    if t == "json":
        return "%s [%s]" % (c["url"], c["field"])
    return c.get("url") or c.get("host") or c.get("command") or ""


# --------------------------------------------------------------------------
# Check runtime state
# --------------------------------------------------------------------------
class Check:
    STATE_FIELDS = ("status", "since", "last_poll", "last_ok", "latency", "detail", "consecutive_fail",
                    "polls", "failures", "history", "probe_state", "last_saved", "last_published", "prev_status")

    def __init__(self, cfg, settings):
        self.cfg = dict(cfg)
        self.cfg.setdefault("interval", settings["interval"])
        self.cfg.setdefault("timeout", settings["timeout"])
        self.cfg.setdefault("fail_after", settings["fail_after"])
        self.name = cfg["name"]
        self.slug = cfg["id"]
        self.type = cfg["type"]
        self.led_name = cfg["led"]
        self.led = parse_led(self.led_name)
        self.interval = float(self.cfg["interval"])
        self.fail_after = int(self.cfg["fail_after"])
        self.target = describe_target(cfg)
        self.signature = json.dumps({k: v for k, v in cfg.items() if k in [f["key"] for f in TYPES[self.type]["fields"]]},
                                    sort_keys=True) + self.type
        self.status = "unknown"
        self.prev_status = None
        self.polling = False
        self.hold_until = 0.0
        self.last_poll = self.last_ok = self.latency = None
        self.detail = ""
        self.since = time.time()
        self.consecutive_fail = self.polls = self.failures = 0
        self.history = deque(maxlen=200)
        self.probe_state = {}
        self.last_saved = self.last_published = 0.0
        self.wake = threading.Event()
        self.stop = threading.Event()

    def adopt(self, old):
        """Carry runtime state across a config reload when the probe is unchanged."""
        for f in self.STATE_FIELDS:
            setattr(self, f, getattr(old, f))

    def to_dict(self):
        return {
            "id": self.slug, "name": self.name, "type": self.type, "target": self.target,
            "led": self.led_name, "row": self.led[0], "col": self.led[1],
            "group": self.cfg.get("group", ""), "location": self.cfg.get("location", ""),
            "status": self.status, "polling": self.polling, "since": self.since,
            "last_poll": self.last_poll, "last_ok": self.last_ok,
            "latency_ms": None if self.latency is None else round(self.latency, 1),
            "detail": self.detail, "interval": self.interval, "polls": self.polls, "failures": self.failures,
            "history": [[round(t), ok, None if ms is None else round(ms, 1)] for t, ok, ms in self.history],
        }


# --------------------------------------------------------------------------
# History database
# --------------------------------------------------------------------------
class History:
    def __init__(self, path, keep_days):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.keep_days = keep_days
        with self.lock, self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS events (ts REAL, check_id TEXT, name TEXT, status TEXT, detail TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS polls (ts REAL, check_id TEXT, ok INTEGER, latency REAL, detail TEXT)")
            self.db.execute("CREATE INDEX IF NOT EXISTS polls_ts ON polls(check_id, ts)")
            self.db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")

    def event(self, check_id, name, status, detail):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (time.time(), check_id, name, status, detail))

    def poll(self, check_id, ok, latency, detail):
        with self.lock, self.db:
            self.db.execute("INSERT INTO polls VALUES (?,?,?,?,?)", (time.time(), check_id, int(ok), latency, detail))

    def events(self, limit=200, check_id=None):
        q, args = "SELECT ts, check_id, name, status, detail FROM events", []
        if check_id:
            q += " WHERE check_id=?"
            args.append(check_id)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        return [dict(zip(("ts", "id", "name", "status", "detail"), r)) for r in rows]

    def polls(self, check_id, hours):
        with self.lock:
            rows = self.db.execute("SELECT ts, ok, latency, detail FROM polls WHERE check_id=? AND ts>? ORDER BY ts",
                                   (check_id, time.time() - hours * 3600)).fetchall()
        return [dict(zip(("ts", "ok", "latency", "detail"), r)) for r in rows]

    def last_status(self):
        with self.lock:
            rows = self.db.execute("SELECT check_id, status, MAX(ts) FROM events GROUP BY check_id").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def prune(self):
        with self.lock, self.db:
            self.db.execute("DELETE FROM polls WHERE ts<?", (time.time() - self.keep_days * 86400,))
            self.db.execute("DELETE FROM events WHERE ts<?", (time.time() - 365 * 86400,))


# --------------------------------------------------------------------------
# Home Assistant via MQTT discovery
# --------------------------------------------------------------------------
class HomeAssistant:
    def __init__(self, cfg, app):
        self.app = app
        self.cfg = cfg
        self.connected = False
        self.client = None
        self.error = ""
        self.announced = set()
        if not cfg.get("enabled"):
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "paho-mqtt not installed"
            log.error("paho-mqtt not installed; Home Assistant reporting disabled")
            return
        self.node = slugify(cfg.get("node_id") or socket.gethostname())
        self.base = cfg.get("base_topic") or "statuspanel/%s" % self.node
        self.prefix = cfg.get("discovery_prefix") or "homeassistant"
        self.avail = self.base + "/availability"
        cid = "statuspanel-%s-%04x" % (self.node, random.randint(0, 0xFFFF))
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)
        except AttributeError:
            self.client = mqtt.Client(client_id=cid)
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password") or None)
        self.client.will_set(self.avail, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(2, 60)
        self.client.connect_async(cfg.get("host", "homeassistant.local"), int(cfg.get("port", 1883)), 30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.error = "connection refused (rc=%s): check username/password" % rc
            log.error("MQTT %s", self.error)
            return
        log.info("MQTT connected")
        self.connected, self.error = True, ""
        client.subscribe(self.prefix + "/status")
        self.announce()

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        if msg.topic == self.prefix + "/status" and msg.payload == b"online":
            threading.Timer(random.uniform(2, 8), self.announce).start()

    def _pub(self, topic, payload, retain=True):
        if self.client:
            if not isinstance(payload, str):
                payload = json.dumps(payload)
            self.client.publish(topic, payload, qos=1, retain=retain)

    def _topics(self, slug):
        return ("%s/binary_sensor/%s/%s/config" % (self.prefix, self.node, slug),
                "%s/sensor/%s/%s_latency/config" % (self.prefix, self.node, slug))

    def announce(self):
        if not self.client:
            return
        device = {"identifiers": ["statuspanel_" + self.node],
                  "name": self.cfg.get("device_name") or "Infrastructure Status Panel",
                  "model": "PiDP-11 status panel", "manufacturer": "DIY", "sw_version": VERSION,
                  "configuration_url": "http://%s.local/" % socket.gethostname()}
        common = {"availability_topic": self.avail, "device": device}
        current = set()
        for c in list(self.app.checks):
            current.add(c.slug)
            st = "%s/%s/state" % (self.base, c.slug)
            t_bin, t_lat = self._topics(c.slug)
            self._pub(t_bin, dict(common, **{
                "name": c.name, "unique_id": "statuspanel_%s_%s" % (self.node, c.slug),
                "object_id": "infra_%s" % c.slug, "device_class": "problem",
                "state_topic": st, "value_template": "{{ 'ON' if value_json.status == 'down' else 'OFF' }}",
                "json_attributes_topic": st, "icon": "mdi:server-network"}))
            self._pub(t_lat, dict(common, **{
                "name": "%s latency" % c.name, "unique_id": "statuspanel_%s_%s_latency" % (self.node, c.slug),
                "object_id": "infra_%s_latency" % c.slug, "state_topic": st,
                "value_template": "{{ value_json.latency_ms }}", "unit_of_measurement": "ms",
                "state_class": "measurement", "entity_category": "diagnostic", "icon": "mdi:timer-outline"}))
        for gone in self.announced - current:  # remove entities for deleted checks
            for t in self._topics(gone):
                self._pub(t, "")
            self._pub("%s/%s/state" % (self.base, gone), "")
        self.announced = current
        sm = self.base + "/summary"
        self._pub("%s/binary_sensor/%s/infrastructure/config" % (self.prefix, self.node), dict(common, **{
            "name": "Infrastructure problem", "unique_id": "statuspanel_%s_any" % self.node,
            "object_id": "infrastructure_problem", "device_class": "problem", "state_topic": sm,
            "value_template": "{{ 'ON' if value_json.down > 0 else 'OFF' }}", "json_attributes_topic": sm}))
        self._pub("%s/sensor/%s/systems_down/config" % (self.prefix, self.node), dict(common, **{
            "name": "Systems down", "unique_id": "statuspanel_%s_down" % self.node,
            "object_id": "infrastructure_systems_down", "state_topic": sm,
            "value_template": "{{ value_json.down }}", "state_class": "measurement", "icon": "mdi:lan-disconnect"}))
        self._pub(self.avail, "online")
        for c in list(self.app.checks):
            self.publish_check(c)
        self.publish_summary()

    def publish_check(self, c):
        if not self.client or c.status == "unknown":
            return
        d = c.to_dict()
        for k in ("history", "polling", "row", "col"):
            d.pop(k)
        self._pub("%s/%s/state" % (self.base, c.slug), d)

    def publish_summary(self):
        if self.client:
            self._pub(self.base + "/summary", self.app.summary())

    def stop(self):
        if self.client:
            self._pub(self.avail, "offline")
            time.sleep(0.3)
            self.client.loop_stop()
            self.client.disconnect()
            self.client = None


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class App:
    def __init__(self, cfg_path):
        self.cfg_path = cfg_path
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.checks = []
        self.cfg = load_config(cfg_path)
        s = self.cfg["settings"]
        self.panel = Panel()
        self.history = History(s["database"], s["history_days"])
        self.started = time.time()
        self.ha = HomeAssistant({}, self)
        self.apply(self.cfg, initial=True)

    # -- configuration ---------------------------------------------------
    def apply(self, cfg, initial=False):
        """Activate a (validated) configuration, reusing state of unchanged checks."""
        with self.lock:
            s = cfg["settings"]
            new_checks = [Check(c, s) for c in cfg["checks"]]
            old = {c.slug: c for c in self.checks}
            prev = self.history.last_status() if initial else {}
            for c in new_checks:
                o = old.get(c.slug)
                if o is not None and o.signature == c.signature:
                    c.adopt(o)
                elif c.slug in prev:
                    c.prev_status, c.since = prev[c.slug][0], prev[c.slug][1]
            for c in self.checks:
                c.stop.set()
                c.wake.set()
            self.min_on = float(s["min_on_ms"]) / 1000.0
            self.blink_half = 0.5 / float(s["blink_hz"])
            self.jitter = min(0.9, max(0.0, float(s["jitter"])))
            self.sample_s = float(s["history_sample_s"])
            self.history.keep_days = float(s["history_days"])
            self.run_led = parse_led(s["run_led"]) if s.get("run_led") else None
            self.alarm_led = parse_led(s["alarm_led"]) if s.get("alarm_led") else None
            self.lamptest = s.get("lamp_test_switch")
            self.pollall = s.get("poll_all_switch")
            ha_changed = initial or cfg["homeassistant"] != self.cfg["homeassistant"]
            self.cfg = cfg
            self.checks = new_checks
            n = max(1, len(new_checks))
            for i, c in enumerate(new_checks):
                delay = (0.5 + 3.0 * i / n) if initial else random.uniform(0, 1)
                threading.Thread(target=self.poller, args=(c, delay), daemon=True, name=c.slug).start()
        if ha_changed:
            self.ha.stop()
            self.ha = HomeAssistant(cfg["homeassistant"], self)
        elif self.ha.connected:
            threading.Thread(target=self.ha.announce, daemon=True).start()
        log.info("configuration active: %d checks", len(new_checks))

    def update_config(self, changes):
        """Validate, save and apply a partial config update from the web editor."""
        with self.lock:
            merged = copy.deepcopy(self.cfg)
            if "checks" in changes:
                merged["checks"] = changes["checks"]
            if "settings" in changes:
                for k in WEB_SETTINGS + ["edit_password"]:
                    if k in changes["settings"]:
                        merged["settings"][k] = changes["settings"][k]
            if "homeassistant" in changes:
                ha = dict(changes["homeassistant"])
                if not ha.get("password"):
                    ha.pop("password", None)  # blank = keep existing secret
                merged["homeassistant"].update({k: v for k, v in ha.items() if k in DEFAULT_HA})
            cfg = normalize_config(merged)
            self._guard_commands(cfg["checks"])
            save_config(self.cfg_path, cfg)
            self.apply(cfg)
            return cfg

    def _guard_commands(self, checks):
        if self.cfg["settings"].get("allow_command_checks_from_web"):
            return
        existing = {c["id"]: c for c in self.cfg["checks"] if c["type"] == "command"}
        for c in checks:
            if c["type"] == "command" and existing.get(c["id"], {}).get("command") != c.get("command"):
                raise ValueError("%s: command checks can only be added or changed in config.json "
                                 "(set allow_command_checks_from_web to permit)" % c["name"])

    def public_config(self):
        cfg = copy.deepcopy(self.cfg)
        s = cfg["settings"]
        cfg["editing"] = {"password_required": bool(s.get("edit_password")),
                          "command_allowed": bool(s.get("allow_command_checks_from_web")),
                          "web_settings": WEB_SETTINGS}
        s.pop("edit_password", None)
        ha = cfg["homeassistant"]
        ha["password_set"] = bool(ha.pop("password", ""))
        cfg["leds"] = list(LEDS)
        cfg["types"] = TYPES
        return cfg

    def check_password(self, given):
        pw = self.cfg["settings"].get("edit_password") or ""
        return not pw or hmac.compare_digest(pw.encode(), (given or "").encode())

    def summary(self):
        checks = list(self.checks)
        down = [c.name for c in checks if c.status == "down"]
        return {"total": len(checks), "down": len(down), "up": sum(c.status == "up" for c in checks),
                "down_systems": down, "updated": time.time()}

    # -- polling ---------------------------------------------------------
    def poller(self, c, initial_delay):
        if c.stop.wait(initial_delay):
            return
        while not (c.stop.is_set() or self.stopping.is_set()):
            t0 = time.monotonic()
            c.polling = True
            c.hold_until = t0 + self.min_on
            try:
                ok, ms, detail = run_probe(c.cfg, c.probe_state)
            except Exception as e:  # noqa: BLE001
                ok, ms, detail = False, None, "probe error: %s" % e
            c.polling = False
            c.hold_until = max(c.hold_until, time.monotonic())
            if c.stop.is_set():
                return
            self.record(c, ok, ms, detail)
            c.wake.clear()
            wait = c.interval * random.uniform(1 - self.jitter, 1 + self.jitter)
            remaining = wait - (time.monotonic() - t0)
            if remaining > 0:
                c.wake.wait(remaining)

    def record(self, c, ok, ms, detail):
        now = time.time()
        c.polls += 1
        c.last_poll, c.latency, c.detail = now, ms, detail
        c.history.append((now, ok, ms))
        if ok:
            c.last_ok, c.consecutive_fail = now, 0
        else:
            c.failures += 1
            c.consecutive_fail += 1
        new = "up" if ok else ("down" if c.consecutive_fail >= c.fail_after else c.status)
        changed = new != c.status
        try:
            if changed or not ok or now - c.last_saved >= self.sample_s:
                c.last_saved = now
                self.history.poll(c.slug, ok, ms, detail)
            if changed:
                old = c.status
                c.status = new
                if not (old == "unknown" and new == c.prev_status):
                    c.since = now
                    self.history.event(c.slug, c.name, new, detail)
                    log.log(logging.WARNING if new == "down" else logging.INFO, "%s is %s (%s)",
                            c.name, new.upper(), detail)
                c.prev_status = None
                self.ha.publish_summary()
        except sqlite3.Error as e:
            log.error("history write failed: %s", e)
        if changed or now - c.last_published >= self.sample_s:
            c.last_published = now
            self.ha.publish_check(c)

    def poll_now(self, slug=None):
        for c in list(self.checks):
            if slug in (None, c.slug):
                c.wake.set()

    # -- LED rendering ---------------------------------------------------
    @staticmethod
    def _switch(spec, sw):
        if not spec:
            return False
        row, bit, level = spec
        return ((sw[row] >> bit) & 1) == level

    def renderer(self):
        last_pollall = False
        while not self.stopping.is_set():
            now = time.monotonic()
            blink_on = int(now / self.blink_half) % 2 == 0
            rows = [0] * 6
            checks = list(self.checks)
            for c in checks:
                if c.polling or now < c.hold_until or (c.status == "down" and blink_on):
                    rows[c.led[0]] |= 1 << c.led[1]
            if self.run_led:
                rows[self.run_led[0]] |= 1 << self.run_led[1]
            if self.alarm_led and any(c.status == "down" for c in checks):
                rows[self.alarm_led[0]] |= 1 << self.alarm_led[1]
            sw = self.panel.switches()
            if self._switch(self.lamptest, sw):
                rows = [0xFFF] * 6
            pa = self._switch(self.pollall, sw)
            if pa and not last_pollall:
                log.info("poll-all switch pressed")
                self.poll_now()
            last_pollall = pa
            self.panel.write(rows)
            time.sleep(0.02)

    def housekeeping(self):
        while not self.stopping.wait(3600):
            try:
                self.history.prune()
            except sqlite3.Error as e:
                log.error("prune failed: %s", e)

    def run(self):
        threading.Thread(target=self.renderer, daemon=True, name="render").start()
        threading.Thread(target=self.housekeeping, daemon=True, name="housekeeping").start()
        port = int(self.cfg["settings"]["web_port"])
        srv = ThreadingHTTPServer(("", port), make_handler(self))
        srv.daemon_threads = True
        log.info("web interface on port %d", port)
        try:
            srv.serve_forever()
        finally:
            self.stopping.set()
            self.ha.stop()


# --------------------------------------------------------------------------
# Web interface
# --------------------------------------------------------------------------
def make_handler(app):
    index_path = os.path.join(HERE, "web", "index.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = "statuspanel/" + VERSION

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1024 * 1024:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _authorized(self):
            if app.check_password(self.headers.get("X-Edit-Password")):
                return True
            self._send(401, {"error": "edit password required"})
            return False

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                with open(index_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                ha = app.ha
                return self._send(200, {
                    "host": socket.gethostname(), "now": time.time(), "started": app.started, "version": VERSION,
                    "summary": app.summary(), "checks": [c.to_dict() for c in list(app.checks)],
                    "run_led": app.cfg["settings"].get("run_led"), "alarm_led": app.cfg["settings"].get("alarm_led"),
                    "panel": {"connected": app.panel.mm is not None, "scan_count": app.panel.scan_count()},
                    "homeassistant": {"enabled": ha.client is not None, "connected": ha.connected, "error": ha.error},
                    "settings": {"min_on_ms": app.min_on * 1000, "blink_hz": 0.5 / app.blink_half},
                })
            if u.path == "/api/config":
                return self._send(200, app.public_config())
            if u.path == "/api/events":
                limit = min(int(q.get("limit", ["200"])[0]), 5000)
                return self._send(200, app.history.events(limit, q.get("id", [None])[0]))
            if u.path == "/api/history":
                return self._send(200, app.history.polls(q.get("id", [""])[0], float(q.get("hours", ["24"])[0])))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/api/poll":
                    app.poll_now(q.get("id", [None])[0])
                    return self._send(200, {"ok": True})
                if u.path == "/api/auth":
                    ok = app.check_password(self.headers.get("X-Edit-Password"))
                    return self._send(200 if ok else 401, {"ok": ok})
                if not self._authorized():
                    return
                body = self._body()
                if u.path == "/api/config":
                    app.update_config(body)
                    return self._send(200, app.public_config())
                if u.path == "/api/test":
                    c = normalize_check(body)
                    app._guard_commands([c])
                    full = Check(c, app.cfg["settings"]).cfg
                    ok, ms, detail = run_probe(full, {})
                    return self._send(200, {"ok": ok, "latency_ms": ms and round(ms, 1), "detail": detail})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            except OSError as e:
                return self._send(500, {"error": "could not save configuration: %s" % e})
            self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(threadName)s: %(message)s")
    if not os.path.exists(args.config):
        example = os.path.join(HERE, "config.example.json")
        log.warning("%s not found; starting from %s", args.config, example)
        with open(example) as f:
            save_config(args.config, normalize_config(json.load(f)))
    App(args.config).run()


if __name__ == "__main__":
    main()
