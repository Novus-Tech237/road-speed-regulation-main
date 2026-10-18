"""
app.py - Road speed regulation server (Flask + PostgreSQL on Neon, or SQLite)

Final-year project: Development of an Intelligent IoT-Based Roadside-to-Vehicle
Communication System for Automatic Vehicle Speed Regulation (Cameroon).

START
    python app.py              -> http://localhost:5000 (this computer only)
    python app.py --lan        -> also reachable by phones on the same Wi-Fi, over HTTPS
                                  (needed for phone GPS; accept the certificate warning once)

Settings are read from config.env next to this file (see config.env.example).

DATABASE
    DATABASE_URL set: the PostgreSQL database it names, normally on Neon. Not set: the local SQLite file
    road_speed.db. See database.py; import_sqlite.py copies an existing road_speed.db into Neon.

ROAD SEGMENTS
    Each road segment is the stretch of road covered by one roadside station (RSU): a position and a
    coverage radius. Every town has an urban segment, and each link of the corridor has one segment per
    side, owned by the nearer town's police. The server finds the station whose zone contains the vehicle
    and returns that segment's limit; outside every zone the vehicle keeps the last limit it received.

MQTT topics (when MQTT_HOST is set)
    corridor/speedevents                      vehicle events (published AND subscribed: hardware
                                              events published here are stored in the database)
    corridor/limits/<segment>                 current limit of a road segment, e.g. dla, dla-ede (retained)
    corridor/rsu/<rsu>/detections             a roadside station detected a vehicle entering its zone
    corridor/vehicles/<CAR-CODE>/limit        limit sent to the vehicle when a station detects it
    corridor/vehicles/<CAR-CODE>/command      warn / reduce / release commands for the vehicle unit
    corridor/signals/<sector>                 police signals for a sector
    corridor/alerts/<sector>                  alerts for significant violations and high-risk situations

SECURITY
- Passwords hashed with werkzeug (scrypt/PBKDF2). Sessions stored in the database.
- 5 wrong passwords lock the code and the IP address for 2 minutes.
- HTTPS is required when the app is used over a network. --lan uses a self-signed
  certificate; for internet use, deploy behind a TLS reverse proxy and set RSR_FORCE_HTTPS=1.
"""
import csv
import io
import json
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

BASE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    """Read KEY=VALUE lines from config.env (environment variables take priority)."""
    path = os.path.join(BASE, "config.env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_config()

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import database  # noqa: E402
from database import connect  # noqa: E402
from send_driver_code import send_account_email, send_driver_code  # noqa: E402

CRED_FILE = os.environ.get("RSR_CRED_FILE", os.path.join(BASE, "first_run_credentials.txt"))
EVENTS_TOPIC = "corridor/speedevents"

MAX_FAILS = 5
LOCK_SECONDS = 120
SESSION_SECONDS = 12 * 3600
LIVE_SECONDS = 15

TOWNS = ["Douala", "Yaoundé", "Edéa", "Mbanga", "Nkongsamba", "Dschang", "Bafoussam"]
# Approximate town-centre coordinates (lat, lon) used to place GPS positions on the trip.
TOWN_COORDS = {"Douala": (4.0511, 9.7679), "Yaoundé": (3.8480, 11.5021), "Edéa": (3.8000, 10.1333),
               "Mbanga": (4.5017, 9.5671), "Nkongsamba": (4.9547, 9.9404), "Dschang": (5.4500, 10.0500),
               "Bafoussam": (5.4781, 10.4176)}

TOWN_CODES = {"Douala": "DLA", "Yaoundé": "YDE", "Edéa": "EDE", "Mbanga": "MBG",
              "Nkongsamba": "NKS", "Dschang": "DSC", "Bafoussam": "BAF"}

AGENCIES = [
    ("GEV", "General Express Voyages", "Active", ""),
    ("FNX", "Finex Voyages", "Active", ""),
    ("GEX", "Guarantee Express", "Active", ""),
    ("BNM", "Binam Voyages", "Active", ""),
    ("TEX", "Touristique Express", "Status unconfirmed", "Suspended in 2023; current status unconfirmed"),
    ("SCF", "Super Comfort", "Active", ""),
]
STATIONS = [
    ("PS-DLA-01", "Commissariat Central n°1 de Douala", "Douala", True),
    ("PS-YDE-01", "Commissariat Central n°1 de Yaoundé", "Yaoundé", True),
    ("PS-YDE-02", "Commissariat Central n°2 de Yaoundé", "Yaoundé", True),
    ("PS-YDE-03", "Commissariat Central n°3 de Yaoundé", "Yaoundé", True),
    ("PS-YDE-04", "Commissariat Central n°4 de Yaoundé", "Yaoundé", True),
    ("PS-EDE-01", "Police station, Edéa", "Edéa", False),
    ("PS-MBG-01", "Police station, Mbanga", "Mbanga", False),
    ("PS-NKS-01", "Police station, Nkongsamba", "Nkongsamba", False),
    ("PS-DSC-01", "Police station, Dschang", "Dschang", False),
    ("PS-BAF-01", "Police station, Bafoussam", "Bafoussam", False),
]
# Only created when SEED_SAMPLE_DRIVERS=1 (sample data, invented names).
SAMPLE_DRIVERS = [
    ("Paul Mbarga", "GEV", "GEV-BUS-07", "paul.mbarga@example.com"),
    ("Aïcha Bello", "FNX", "FNX-BUS-12", "aicha.bello@example.com"),
    ("Emmanuel Tchoua", "GEX", "GEX-BUS-03", "emmanuel.tchoua@example.com"),
]
# Starting limits until police set their own (km/h).
DEFAULT_LIMITS = {"Douala": 50, "Yaoundé": 50, "Edéa": 60, "Mbanga": 60,
                  "Nkongsamba": 60, "Dschang": 40, "Bafoussam": 50}

# Road segments and roadside detection
CORRIDOR = ["Bafoussam", "Dschang", "Nkongsamba", "Mbanga", "Douala", "Edéa", "Yaoundé"]  # order along the road
URBAN_RADIUS_KM = 10      # coverage zone of the roadside station of each town
OPEN_ROAD_LIMIT = 80      # placeholder for open-road segments until an administrator sets the official limit
HOLD_SECONDS = 120        # outside every zone, a vehicle keeps the last limit it received for this long
REDUCE_RATE_KMH_S = 6     # progressive reduction step sent to the vehicle unit

# Logging, alerts and history
TELEMETRY_SECONDS = 5     # interval of the speed and location samples kept for the history
TELEMETRY_DAYS = int(os.environ.get("RSR_TELEMETRY_DAYS", "180"))
SUSTAINED_SECONDS = 30    # a vehicle over the limit for this long raises an alert
REPEAT_WINDOW, REPEAT_COUNT = 900, 3   # this many violations by one driver within 15 min raise an alert
MOVING_KMH = 5            # history statistics ignore samples of a stopped vehicle
LEVEL_RANK = {"medium": 1, "high": 2, "critical": 3}

PREFIX = {"driver": "DRV-", "police": "PS-", "admin": "ADM-"}
ROLE_LABEL = {"driver": "a driver", "police": "a police station", "admin": "an admin"}

app = Flask(__name__, static_folder=None)


def now():
    return time.time()


def iso(ts=None):
    return datetime.fromtimestamp(ts or now(), tz=timezone.utc).isoformat()


def slug(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def severity(speed, limit):
    if not limit or speed <= limit:
        return "none"
    over = (speed - limit) / limit
    return "low" if over <= 0.10 else "medium" if over <= 0.25 else "high"


def num(value, default=0.0):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def position(d):
    """(lat, lon) from a request body, or None when absent or invalid."""
    lat, lon = num(d.get("lat"), None), num(d.get("lon"), None)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def distance_km(a, b):
    """Great-circle distance between two (lat, lon) points."""
    r = math.pi / 180
    h = (math.sin((b[0] - a[0]) * r / 2) ** 2
         + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin((b[1] - a[1]) * r / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def percentile(sorted_values, pct):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo), 1)


# ---------------------------------------------------------------- database (tables and connections: database.py)
def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


PASSWORD_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L look-alikes and no - or _: read from an email, typed on a phone


def new_password():
    return "".join(secrets.choice(PASSWORD_CHARS) for _ in range(12))


def new_driver_code(conn, agency):
    while True:
        code = f"DRV-{agency}-{secrets.randbelow(9000) + 1000}"
        if not conn.execute("SELECT 1 FROM drivers WHERE code=?", (code,)).fetchone():
            return code


def init_db():
    conn = connect()
    first_run = database.create_schema(conn)
    if first_run:
        issued, t = [], now()
        for row in AGENCIES:
            conn.execute("INSERT INTO agencies VALUES (?,?,?,?)", row)
        for code, name, sector, verified in STATIONS:
            pw = new_password()
            conn.execute("INSERT INTO stations (code, name, sector, verified, pw_hash) VALUES (?,?,?,?,?)",
                         (code, name, sector, int(verified), generate_password_hash(pw)))
            conn.execute("INSERT INTO issued_credentials VALUES (?,?,?)", (code, pw, t))
            issued.append(("Police", code, pw, name + ("" if verified else "  [To confirm]")))
        admins = [("ADM-SUPER", "Super admin", None)] + [(f"ADM-{c}", f"{n} admin", c) for c, n, _s, _n in AGENCIES]
        for code, name, agency in admins:
            pw = new_password()
            conn.execute("INSERT INTO admins (code, name, agency, pw_hash) VALUES (?,?,?,?)",
                         (code, name, agency, generate_password_hash(pw)))
            conn.execute("INSERT INTO issued_credentials VALUES (?,?,?)", (code, pw, t))
            issued.append(("Admin", code, pw, name))
        if os.environ.get("SEED_SAMPLE_DRIVERS") == "1":
            for name, agency, car, email in SAMPLE_DRIVERS:
                pw, code = new_password(), new_driver_code(conn, agency)
                conn.execute("INSERT INTO drivers VALUES (?,?,?,?,?,?,?,?,1)",
                             (code, name, email, agency, car, generate_password_hash(pw), t, "sample"))
                conn.execute("INSERT INTO issued_credentials VALUES (?,?,?)", (code, pw, t))
                issued.append(("Driver (sample)", code, pw, f"{name} | {agency} | car {car}"))
        for sector, lim in DEFAULT_LIMITS.items():
            conn.execute("INSERT INTO sector_limits VALUES (?,?,?,?)", (sector, lim, "default", t))
        conn.commit()
        with open(CRED_FILE, "w", encoding="utf-8") as f:
            f.write("ROAD SPEED REGULATION - ONE-TIME CREDENTIALS FILE\n")
            f.write(f"Generated {iso(t)}. Hand these to each station/admin, then DELETE this file.\n")
            f.write("Each person should change their password after the first sign-in.\n\n")
            for role, code, pw, label in issued:
                f.write(f"{role:<16} {code:<14} {pw:<16} {label}\n")
        print(f"First run: credentials written to {CRED_FILE}")
    if not conn.execute("SELECT 1 FROM segments").fetchone():
        seed_segments(conn)
    conn.execute("DELETE FROM telemetry WHERE ts<?", (now() - TELEMETRY_DAYS * 86400,))
    conn.commit()
    conn.close()


def seed_segments(conn):
    """Create the road segments, each covered by one roadside station: one urban segment per town (keeping the
    limit its police already set) and, on each corridor link, one segment per side owned by the nearer town."""
    t = now()
    town_limit = {r["sector"]: r["limit_kmh"] for r in conn.execute("SELECT * FROM sector_limits")}
    rows = [(TOWN_CODES[town], f"{town} urban area", town, *TOWN_COORDS[town], URBAN_RADIUS_KM,
             town_limit.get(town, DEFAULT_LIMITS[town])) for town in TOWNS]
    for a, b in zip(CORRIDOR, CORRIDOR[1:]):
        (la, oa), (lb, ob) = TOWN_COORDS[a], TOWN_COORDS[b]
        radius = round(distance_km((la, oa), (lb, ob)) / 4 * 1.15, 1)  # each half of the link, with some overlap
        for near, far, f in ((a, b, 0.25), (b, a, 0.75)):
            rows.append((f"{TOWN_CODES[near]}-{TOWN_CODES[far]}", f"{near} – {far} road, {near} side", near,
                         round(la + (lb - la) * f, 5), round(oa + (ob - oa) * f, 5), radius, OPEN_ROAD_LIMIT))
    conn.executemany("INSERT INTO segments (code, name, sector, rsu, lat, lon, radius_km, limit_kmh, set_by, updated)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                     [(code, name, sector, f"RSU-{code}", lat, lon, radius, lim, "default", t)
                      for code, name, sector, lat, lon, radius, lim in rows])
    for town in TOWNS:  # events stored before segments existed belong to their town's urban segment
        conn.execute("UPDATE events SET segment=? WHERE segment IS NULL AND sector=?", (TOWN_CODES[town], town))


def segment_rows(sector=None):
    q = "SELECT * FROM segments" + (" WHERE sector=?" if sector else "") + " ORDER BY sector, length(code), code"
    return [dict(r) for r in db().execute(q, (sector,) if sector else ())]


def get_segment(conn, code):
    return conn.execute("SELECT * FROM segments WHERE code=?", ((code or "").upper(),)).fetchone()


def detect_segment(conn, pos):
    """FR1: the roadside station whose coverage zone contains the position, as (segment, distance in km).
    Where zones overlap, the smallest zone wins (a town's station over the open-road one around it)."""
    best = (None, None)
    if pos:
        for r in conn.execute("SELECT * FROM segments"):
            dkm = distance_km(pos, (r["lat"], r["lon"]))
            if dkm <= r["radius_km"] and (best[0] is None or (r["radius_km"], dkm) < (best[0]["radius_km"], best[1])):
                best = (r, dkm)
    return best


# ---------------------------------------------------------------- MQTT
_mqtt = None


def mqtt_publish(topic, payload, retain=False):
    if _mqtt is None:
        return False
    try:
        _mqtt.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=retain)
        return True
    except Exception:  # noqa: BLE001
        return False


def store_event(conn, p, source):
    """Insert an event (dict in the corridor/speedevents format) with its speed, location and time, then check it
    against the alert rules. Ignores duplicates, so a vehicle can safely resend events it was not sure were received.
    Optional fields: lat, lon, segment (else found from the position) and device_ts (ms, when it happened)."""
    speed, limit = num(p.get("speed_kmh")), num(p.get("limit_kmh"))
    p.setdefault("severity", severity(speed, limit))
    p["event_id"] = p.get("event_id") or str(uuid.uuid4())
    ag = (p.get("agency_code") or "").upper()
    if not ag and p.get("agency"):
        r = conn.execute("SELECT code FROM agencies WHERE code=? OR name=?", (str(p["agency"]).upper(), p["agency"])).fetchone()
        ag = r["code"] if r else str(p["agency"])[:40]
    pos = position(p)
    seg = get_segment(conn, p.get("segment")) or detect_segment(conn, pos)[0]
    sector = p.get("sector") or (seg["sector"] if seg else None)
    received, happened = now(), num(p.get("device_ts"), 0) / 1000
    ts = happened if received - 86400 < happened <= received + 300 else received
    ev = dict(event_id=p["event_id"], ts=ts, received=received, agency=ag, driver_code=p.get("driver_code"),
              driver_name=p.get("driver_name"), car_code=p.get("car_code"), sector=sector,
              segment=seg["code"] if seg else None, lat=pos[0] if pos else None, lon=pos[1] if pos else None,
              speed=speed, limit_kmh=limit, severity=p["severity"], speed_reduced=int(bool(p.get("speed_reduced"))),
              kind=p.get("type") or "warning", source=source, payload=json.dumps(p, ensure_ascii=False))
    cur = conn.execute(f"INSERT INTO events ({', '.join(ev)}) VALUES ({', '.join('?' * len(ev))}) ON CONFLICT DO NOTHING",
                       tuple(ev.values()))
    if cur.rowcount:
        check_event_alerts(conn, ev)
    conn.commit()


# ---------------------------------------------------------------- alerts (FR10)
def raise_alert(conn, level, kind, message, ev):
    """Store an alert and publish it on corridor/alerts/<sector>."""
    row = dict(ts=now(), level=level, kind=kind, sector=ev.get("sector"), segment=ev.get("segment"),
               driver_code=ev.get("driver_code"), driver_name=ev.get("driver_name"), agency=ev.get("agency"),
               car_code=ev.get("car_code"), speed=ev.get("speed"), limit_kmh=ev.get("limit_kmh"), message=message,
               event_id=ev.get("event_id"))
    aid = conn.execute(f"INSERT INTO alerts ({', '.join(row)}) VALUES ({', '.join('?' * len(row))}) RETURNING id",
                       tuple(row.values())).fetchone()[0]
    mqtt_publish(f"corridor/alerts/{slug(row['sector'] or 'unknown')}", {**row, "id": aid, "ts": iso(row["ts"])})


def recent_alert(conn, driver_code, kinds, seconds, min_rank=0):
    rows = conn.execute(f"SELECT level FROM alerts WHERE driver_code=? AND ts>? AND kind IN ({','.join('?' * len(kinds))})",
                        (driver_code, now() - seconds, *kinds)).fetchall()
    return any(LEVEL_RANK.get(r["level"], 0) >= min_rank for r in rows)


def check_event_alerts(conn, ev):
    """Alert rules for a stored violation event:
    critical  extreme speed: 50% or more, or 40 km/h or more, over the limit
    high      high-severity violation (more than 25% over the limit)
    medium    repeat offender: REPEAT_COUNT violations by the same driver within REPEAT_WINDOW
    A warning and the speed reduction that follows belong to one episode, so they raise one speed alert, unless
    the second one is worse. Sustained speeding is checked on each vehicle heartbeat (see driver_tick)."""
    speed, limit = ev["speed"], ev["limit_kmh"]
    if not limit or speed <= limit:
        return
    level = None
    if speed >= limit * 1.5 or speed >= limit + 40:
        level, kind, text = "critical", "extreme_speed", f"Extreme speed: {speed:.0f} km/h in a {limit:.0f} km/h segment"
    elif ev["severity"] == "high":
        level, kind, text = "high", "high_severity", f"High-severity violation: {speed:.0f} km/h in a {limit:.0f} km/h segment"
    if level and not recent_alert(conn, ev["driver_code"], ("extreme_speed", "high_severity"), 120, LEVEL_RANK[level]):
        raise_alert(conn, level, kind, text, ev)
    if ev["kind"] == "warning" and ev["driver_code"]:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE driver_code=? AND kind='warning' AND ts>?",
                         (ev["driver_code"], now() - REPEAT_WINDOW)).fetchone()[0]
        if n >= REPEAT_COUNT and not recent_alert(conn, ev["driver_code"], ("repeat_offender",), REPEAT_WINDOW):
            raise_alert(conn, "medium", "repeat_offender", f"Repeat offender: {n} violations in {REPEAT_WINDOW // 60} min", ev)


def alert_rows(sector=None, agency=None, hours=24):
    q, args = "SELECT * FROM alerts WHERE ts>?", [now() - hours * 3600]
    if sector:
        q, args = q + " AND sector=?", args + [sector]
    if agency:
        q, args = q + " AND agency=?", args + [agency]
    return [dict(r) for r in db().execute(q + " ORDER BY (ack_ts IS NOT NULL), ts DESC LIMIT 50", args)]


def publish_limit(seg, lim, by):
    mqtt_publish(f"corridor/limits/{seg['code'].lower()}",
                 {"segment": seg["code"], "name": seg["name"], "sector": seg["sector"], "rsu": seg["rsu"],
                  "limit_kmh": lim, "set_by": by, "ts": iso()}, retain=True)


def set_segment_limit(conn, seg, lim, by, radius=None):
    """FR12: store a road segment's limit (and coverage radius), keep an audit trail, and send it to the station."""
    conn.execute("UPDATE segments SET limit_kmh=?, radius_km=?, set_by=?, updated=? WHERE code=?",
                 (lim, seg["radius_km"] if radius is None else radius, by, now(), seg["code"]))
    if lim != seg["limit_kmh"]:
        conn.execute("INSERT INTO limit_changes (ts, segment, old_limit, new_limit, set_by) VALUES (?,?,?,?,?)",
                     (now(), seg["code"], seg["limit_kmh"], lim, by))
    conn.commit()
    publish_limit(seg, lim, by)


def start_mqtt():
    global _mqtt
    if not os.environ.get("MQTT_HOST"):
        return
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError:
        print("MQTT_HOST is set but paho-mqtt is not installed (pip install paho-mqtt).")
        return
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"rsr-server-{secrets.token_hex(3)}")
    except AttributeError:
        client = mqtt.Client(client_id=f"rsr-server-{secrets.token_hex(3)}")
    if os.environ.get("MQTT_USER"):
        client.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASS"))
    if os.environ.get("MQTT_TLS") == "1":
        client.tls_set()

    def on_connect(c, *_args):
        c.subscribe(EVENTS_TOPIC, qos=1)
        conn = connect()
        for r in conn.execute("SELECT * FROM segments"):
            c.publish(f"corridor/limits/{r['code'].lower()}", json.dumps(
                {"segment": r["code"], "name": r["name"], "sector": r["sector"], "rsu": r["rsu"],
                 "limit_kmh": r["limit_kmh"], "ts": iso(r["updated"])}, ensure_ascii=False), qos=1, retain=True)
        conn.close()
        print(f"MQTT connected; subscribed to {EVENTS_TOPIC}")

    def on_message(_c, _u, msg):
        try:
            p = json.loads(msg.payload.decode("utf-8"))
            if isinstance(p, dict) and p.get("source") != "server":
                conn = connect()
                store_event(conn, p, "mqtt")
                conn.close()
        except Exception as exc:  # noqa: BLE001
            print("Ignored MQTT message:", exc)

    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", "1883")))
        client.loop_start()
        _mqtt = client
    except Exception as exc:  # noqa: BLE001
        print("MQTT connection failed:", exc)


# ---------------------------------------------------------------- security helpers
FAILS = {}
FAILS_LOCK = threading.Lock()


def role_error(role, code):
    c = (code or "").strip().upper()
    for other, prefix in PREFIX.items():
        if other != role and c.startswith(prefix):
            return f"That is {ROLE_LABEL[other]} code. It cannot be used here."
    if not c.startswith(PREFIX[role]):
        return f"{ROLE_LABEL[role][2:].strip().capitalize()} codes start with {PREFIX[role]}"
    return None


def locked(key):
    rec = FAILS.get(key)
    return int(rec[1] - now()) + 1 if rec and rec[1] > now() else 0


def register_fail(*keys):
    with FAILS_LOCK:
        for key in keys:
            rec = FAILS.setdefault(key, [0, 0])
            if rec[1] and rec[1] <= now():
                rec[:] = [0, 0]
            rec[0] += 1
            if rec[0] >= MAX_FAILS:
                rec[1] = now() + LOCK_SECONDS


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


def auth(*roles):
    token = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if not token:
        return None
    r = db().execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not r or r["exp"] < now() or (roles and r["role"] not in roles):
        return None
    return {"role": r["role"], "token": token, **json.loads(r["data"])}


def err(message, status=400, **extra):
    return jsonify({"ok": False, "error": message, **extra}), status


def need(sess, role_msg):
    return err(role_msg, 401) if not sess else None


@app.before_request
def force_https():
    if os.environ.get("RSR_FORCE_HTTPS") == "1":
        if request.headers.get("X-Forwarded-Proto", request.scheme) != "https":
            return redirect(request.url.replace("http://", "https://", 1), code=301)
    return None


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    if request.headers.get("X-Forwarded-Proto", request.scheme) == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def email_ready():
    return bool(os.environ.get("SMTP_HOST") or os.environ.get("BREVO_API_KEY"))


def app_url():
    """Public address of the app for links in emails: APP_URL when set, else the address of this request."""
    url = os.environ.get("APP_URL", "").strip().rstrip("/")
    if url:
        return url
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    return f"{proto}://{request.host}"


def sign_in_link(role, **fields):
    """Link that opens the sign-in form with these fields filled in. They travel in the URL fragment,
    which the browser keeps to itself (it is not sent to the server or written in its logs)."""
    return f"{app_url()}/#signin?{urlencode(dict(role=role, **fields))}"


def driver_link(code, agency, car_code, name):
    return sign_in_link("driver", code=code, agency=agency, car=car_code, name=name)


def mail(email, name, code, agency, car_code, temp_password=None):
    """Send the driver email with every detail the driver sign-in asks for; returns (via, error)."""
    try:
        return send_driver_code(email, name, code, agency_name(agency), temp_password=temp_password, car_code=car_code,
                                sign_in_url=driver_link(code, agency, car_code, name)), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def same_name(a, b):
    """Names match ignoring case, accents and extra spaces ("aicha  bello" signs in as "Aïcha Bello")."""
    def norm(s):
        s = unicodedata.normalize("NFKD", s or "")
        return " ".join("".join(ch for ch in s if not unicodedata.combining(ch)).casefold().split())
    return norm(a) == norm(b)


def password_ok(pw_hash, pw):
    """Also accepts the password with spaces around it, as often happens when it is copied from an email."""
    return check_password_hash(pw_hash, pw) or (pw != pw.strip() and check_password_hash(pw_hash, pw.strip()))


def must_change(code):
    """Still using a temporary password issued by the system or an admin."""
    return bool(db().execute("SELECT 1 FROM issued_credentials WHERE code=?", (code,)).fetchone())


def agency_name(code):
    r = db().execute("SELECT name FROM agencies WHERE code=?", (code,)).fetchone()
    return r["name"] if r else code


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "mode": "server", "mqtt": _mqtt is not None,
                    "email": email_ready(), "database": "postgres" if database.POSTGRES else "sqlite",
                    "https": request.headers.get("X-Forwarded-Proto", request.scheme) == "https"})


@app.get("/api/meta")
def meta():
    c = db()
    return jsonify({"ok": True, "towns": TOWNS, "town_coords": TOWN_COORDS,
                    "agencies": [dict(r) for r in c.execute("SELECT * FROM agencies")],
                    "stations": [dict(code=r["code"], name=r["name"], sector=r["sector"], verified=bool(r["verified"]))
                                 for r in c.execute("SELECT * FROM stations")],
                    "segments": [{k: g[k] for k in ("code", "name", "sector", "rsu", "lat", "lon", "radius_km", "limit_kmh")}
                                 for g in segment_rows()]})


# ---------------------------------------------------------------- sign up / log in / password
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/signup")
def signup():
    d = request.get_json(silent=True) or {}
    name, email = (d.get("name") or "").strip(), (d.get("email") or "").strip().lower()
    agency, car = (d.get("agency") or "").strip().upper(), (d.get("car_code") or "").strip().upper()
    pw, pw2 = d.get("password") or "", d.get("password2") or ""
    c = db()
    if len(name) < 3:
        return err("Enter your full name.")
    if not EMAIL_RE.match(email):
        return err("Enter a valid email address.")
    if not c.execute("SELECT 1 FROM agencies WHERE code=?", (agency,)).fetchone():
        return err("Choose your travel agency.")
    if len(car) < 3:
        return err("Enter the car code of your vehicle.")
    if len(pw) < 6:
        return err("Password must be at least 6 characters.")
    if pw != pw2:
        return err("The two passwords do not match.")
    if c.execute("SELECT 1 FROM drivers WHERE email=?", (email,)).fetchone():
        return err("An account already uses this email.")
    code = new_driver_code(c, agency)
    via, problem = mail(email, name, code, agency, car)
    if problem:
        return err(f"The email could not be sent ({problem}). The account was not created.", 502)
    c.execute("INSERT INTO drivers VALUES (?,?,?,?,?,?,?,?,0)",
              (code, name, email, agency, car, generate_password_hash(pw), now(), "self"))
    c.commit()
    return jsonify({"ok": True, "delivered_via": via, "message": f"Account created. Your driver code was sent to {email}."})


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    role = d.get("role")
    if role not in PREFIX:
        return err("Unknown role.")
    code, pw = (d.get("code") or "").strip().upper(), d.get("password") or ""
    msg = role_error(role, code)
    if msg:
        return err(msg, 403)
    key, ipkey = f"{role}:{code}", f"ip:{client_ip()}"
    wait = max(locked(key), locked(ipkey))
    if wait:
        return err(f"Too many wrong attempts. Try again in {wait} s.", 429)
    c, profile, mismatch = db(), None, None
    if role == "driver":
        r = c.execute("SELECT * FROM drivers WHERE code=?", (code,)).fetchone()
        if r and password_ok(r["pw_hash"], pw):
            # The code and password prove who this is, so say which of the other details is wrong.
            if r["agency"] != (d.get("agency") or "").upper():
                mismatch = "agency"
            elif r["car_code"] != (d.get("car_code") or "").strip().upper():
                mismatch = "car code"
            elif not same_name(r["name"], d.get("name")):
                mismatch = "name"
            else:
                t_from, t_to = d.get("trip_from"), d.get("trip_to")
                if t_from not in TOWNS or t_to not in TOWNS or t_from == t_to:
                    return err("Choose two different towns for your trip.")
                profile = dict(code=code, name=r["name"], agency=r["agency"], agency_name=agency_name(r["agency"]),
                               car_code=r["car_code"], trip_from=t_from, trip_to=t_to)
    elif role == "police":
        r = c.execute("SELECT * FROM stations WHERE code=?", (code,)).fetchone()
        if r and password_ok(r["pw_hash"], pw):
            profile = dict(code=code, name=r["name"], sector=r["sector"], verified=bool(r["verified"]))
    else:
        r = c.execute("SELECT * FROM admins WHERE code=?", (code,)).fetchone()
        if r and password_ok(r["pw_hash"], pw):
            profile = dict(code=code, name=r["name"], agency=r["agency"],
                           agency_name=agency_name(r["agency"]) if r["agency"] else None, super=r["agency"] is None)
    if not profile:
        register_fail(key, ipkey)
        left = max(0, MAX_FAILS - FAILS.get(key, [0])[0])
        first = (f"Your code and password are right, but the {mismatch} is not the one on your account. "
                 f"Enter the {mismatch} written in your welcome email." if mismatch
                 else "Sign-in details do not match our records.")
        return err(first + (f" {left} attempt(s) left before a {LOCK_SECONDS} s lock." if left else f" Locked for {LOCK_SECONDS} s."), 401)
    FAILS.pop(key, None)
    token = secrets.token_urlsafe(32)
    c.execute("DELETE FROM sessions WHERE exp<?", (now(),))
    c.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token, role, json.dumps(profile), now() + SESSION_SECONDS))
    c.commit()
    return jsonify({"ok": True, "token": token, "role": role, "profile": profile, "must_change_password": must_change(code)})


@app.get("/api/session")
def session_info():
    s = auth()
    if not s:
        return err("Not signed in.", 401)
    return jsonify({"ok": True, "role": s["role"], "profile": {k: v for k, v in s.items() if k not in ("role", "token")},
                    "must_change_password": must_change(s["code"])})


@app.post("/api/logout")
def logout():
    s = auth()
    if s:
        db().execute("DELETE FROM sessions WHERE token=?", (s["token"],))
        db().commit()
    return jsonify({"ok": True})


@app.post("/api/password")
def change_password():
    s = auth()
    if not s:
        return err("Sign in first.", 401)
    d = request.get_json(silent=True) or {}
    old, new, new2 = d.get("old") or "", d.get("new") or "", d.get("new2") or ""
    table = {"driver": "drivers", "police": "stations", "admin": "admins"}[s["role"]]
    c = db()
    r = c.execute(f"SELECT pw_hash FROM {table} WHERE code=?", (s["code"],)).fetchone()  # noqa: S608
    if not r or not password_ok(r["pw_hash"], old):
        return err("The current password is wrong.")
    if len(new) < 6:
        return err("The new password must be at least 6 characters.")
    if new != new2:
        return err("The two new passwords do not match.")
    c.execute(f"UPDATE {table} SET pw_hash=? WHERE code=?", (generate_password_hash(new), s["code"]))  # noqa: S608
    c.execute("DELETE FROM issued_credentials WHERE code=?", (s["code"],))
    c.commit()
    return jsonify({"ok": True, "message": "Password changed."})


# ---------------------------------------------------------------- admin
def driver_rows(agency=None):
    c = db()
    q = ("SELECT d.*, i.password AS issued FROM drivers d LEFT JOIN issued_credentials i ON i.code=d.code"
         + (" WHERE d.agency=?" if agency else "") + " ORDER BY d.created DESC")
    live = {r["driver_code"]: r["updated"] for r in c.execute("SELECT driver_code, updated FROM live")}
    return [dict(code=r["code"], name=r["name"], email=r["email"], agency=r["agency"], car_code=r["car_code"],
                 source=r["source"], sample=bool(r["sample"]), issued_password=r["issued"],
                 live=now() - live.get(r["code"], 0) < LIVE_SECONDS)
            for r in c.execute(q, (agency,) if agency else ())]


@app.post("/api/admin/drivers")
def admin_register_driver():
    s = auth("admin")
    if not s:
        return err("Sign in as an admin first.", 401)
    d = request.get_json(silent=True) or {}
    agency = s["agency"] or (d.get("agency") or "").upper()
    name, email, car = (d.get("name") or "").strip(), (d.get("email") or "").strip().lower(), (d.get("car_code") or "").strip().upper()
    c = db()
    if not c.execute("SELECT 1 FROM agencies WHERE code=?", (agency,)).fetchone():
        return err("Choose an agency.")
    if len(name) < 3 or not EMAIL_RE.match(email) or len(car) < 3:
        return err("Enter the driver's full name, a valid email and a car code.")
    if c.execute("SELECT 1 FROM drivers WHERE email=?", (email,)).fetchone():
        return err("An account already uses this email.")
    code, pw = new_driver_code(c, agency), new_password()
    c.execute("INSERT INTO drivers VALUES (?,?,?,?,?,?,?,?,0)", (code, name, email, agency, car, generate_password_hash(pw), now(), s["code"]))
    c.execute("INSERT INTO issued_credentials VALUES (?,?,?)", (code, pw, now()))
    c.commit()
    # The account exists whatever happens to the email: the admin can see the temporary password anyway, so when
    # the email cannot go out they hand the details over, or use Reset password to email new ones later.
    via, problem = mail(email, name, code, agency, car, temp_password=pw)
    if problem:
        message = (f"Driver {code} registered, but the email to {email} could not be sent ({problem}). "
                   "Give the driver these details yourself, or use Reset password later to email new ones.")
    elif via == "console":
        message = (f"Driver {code} registered. Email sending is not set up on the server, so nothing was sent to "
                   f"{email}. Give the driver these details yourself.")
    else:
        message = f"Driver {code} registered. Code and temporary password emailed to {email}."
    return jsonify({"ok": True, "code": code, "password": pw, "sign_in_url": driver_link(code, agency, car, name),
                    "delivered_via": via, "email_failed": bool(problem), "message": message})


@app.get("/api/admin/drivers")
def admin_list_drivers():
    s = auth("admin")
    if not s:
        return err("Sign in as an admin first.", 401)
    return jsonify({"ok": True, "drivers": driver_rows(s["agency"])})


@app.delete("/api/admin/drivers/<code>")
def admin_delete_driver(code):
    s = auth("admin")
    if not s:
        return err("Sign in as an admin first.", 401)
    c, code = db(), code.upper()
    r = c.execute("SELECT agency FROM drivers WHERE code=?", (code,)).fetchone()
    if not r or (s["agency"] and r["agency"] != s["agency"]):
        return err("Driver not found in your agency.", 404)
    for t in ("drivers", "issued_credentials"):
        c.execute(f"DELETE FROM {t} WHERE code=?", (code,))  # noqa: S608
    c.execute("DELETE FROM live WHERE driver_code=?", (code,))
    c.execute("DELETE FROM sessions WHERE role='driver' AND data LIKE ?", (f'%"{code}"%',))
    c.commit()
    return jsonify({"ok": True, "message": f"Driver {code} removed. Their event history is kept."})


@app.post("/api/admin/accounts/<code>/reset")
def admin_reset(code):
    """Issue a new temporary password. Drivers: emailed. Stations/admins: shown to the super admin."""
    s = auth("admin")
    if not s:
        return err("Sign in as an admin first.", 401)
    c, code, pw = db(), code.upper(), new_password()
    if code.startswith("DRV-"):
        r = c.execute("SELECT * FROM drivers WHERE code=?", (code,)).fetchone()
        if not r or (s["agency"] and r["agency"] != s["agency"]):
            return err("Driver not found in your agency.", 404)
        via, problem = mail(r["email"], r["name"], code, r["agency"], r["car_code"], temp_password=pw)
        if problem:
            return err(f"The email could not be sent ({problem}). Password not changed.", 502)
        c.execute("UPDATE drivers SET pw_hash=? WHERE code=?", (generate_password_hash(pw), code))
        msg = (f"New temporary password emailed to {r['email']}." if via != "console" else
               f"Email sending is not set up, so nothing was sent. New temporary password for {code}: {pw}  "
               "(hand it over privately).")
    else:
        if s["agency"]:
            return err("Only the super admin can reset station and admin passwords.", 403)
        table = "stations" if code.startswith("PS-") else "admins"
        if not c.execute(f"SELECT 1 FROM {table} WHERE code=?", (code,)).fetchone():  # noqa: S608
            return err("Account not found.", 404)
        c.execute(f"UPDATE {table} SET pw_hash=? WHERE code=?", (generate_password_hash(pw), code))  # noqa: S608
        msg, via = f"New temporary password for {code}: {pw}  (hand it over privately).", "screen"
    c.execute(database.upsert("issued_credentials", "code, password, issued", "code"), (code, pw, now()))
    c.execute("DELETE FROM sessions WHERE data LIKE ?", (f'%"{code}"%',))
    c.commit()
    return jsonify({"ok": True, "message": msg, "delivered_via": via})


@app.post("/api/admin/stations/<code>")
def admin_edit_station(code):
    s = auth("admin")
    if not s or s["agency"]:
        return err("Only the super admin can edit police stations.", 403)
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if len(name) < 4:
        return err("Enter the official station name.")
    c = db()
    if not c.execute("SELECT 1 FROM stations WHERE code=?", (code.upper(),)).fetchone():
        return err("Station not found.", 404)
    c.execute("UPDATE stations SET name=?, verified=? WHERE code=?", (name, int(bool(d.get("verified"))), code.upper()))
    c.commit()
    return jsonify({"ok": True, "message": "Station updated."})


def super_only():
    s = auth("admin")
    if not s:
        return None, err("Sign in as an admin first.", 401)
    if s["agency"]:
        return None, err("Only the super admin can create police station and admin accounts.", 403)
    return s, None


def deliver_account(email, name, role_label, code, pw, sign_in_as):
    """Email the new account if an address was given. Returns (note, error)."""
    if not email:
        return "No email given: hand the code and password over in person.", None
    via, problem = None, None
    try:
        via = send_account_email(email, name, role_label, code, pw, sign_in_as,
                                 sign_in_url=sign_in_link("police" if code.startswith("PS-") else "admin", code=code))
    except Exception as exc:  # noqa: BLE001
        problem = str(exc)
    if problem:
        return None, problem
    return (f"Details emailed to {email}." if via != "console"
            else "Email sending is not set up, so the email was written in the server window."), None


@app.post("/api/admin/stations")
def admin_create_station():
    s, e = super_only()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    name, sector = (d.get("name") or "").strip(), d.get("sector")
    email = (d.get("email") or "").strip().lower()
    if len(name) < 4:
        return err("Enter the station name.")
    if sector not in TOWNS:
        return err("Choose the town (sector) of the station.")
    if email and not EMAIL_RE.match(email):
        return err("Enter a valid email address, or leave it empty.")
    c, prefix = db(), f"PS-{TOWN_CODES[sector]}-"
    used = [int(r["code"][len(prefix):]) for r in c.execute("SELECT code FROM stations WHERE code LIKE ?", (prefix + "%",))
            if r["code"][len(prefix):].isdigit()]
    code, pw = f"{prefix}{(max(used) if used else 0) + 1:02d}", new_password()
    note, problem = deliver_account(email, name, "police station", code, pw, "Police station")
    if problem:
        return err(f"The email could not be sent ({problem}). The station was not created.", 502)
    c.execute("INSERT INTO stations (code, name, sector, verified, pw_hash, email) VALUES (?,?,?,?,?,?)",
              (code, name, sector, int(bool(d.get("verified"))), generate_password_hash(pw), email or None))
    c.execute("INSERT INTO issued_credentials VALUES (?,?,?)", (code, pw, now()))
    c.commit()
    return jsonify({"ok": True, "code": code, "password": pw,
                    "message": f"Police station created: {code}, temporary password {pw}. {note}"})


@app.post("/api/admin/agencies")
def admin_create_agency():
    """Create a travel agency together with its admin account (ADM-<CODE>)."""
    s, e = super_only()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    code = re.sub(r"[^A-Z]", "", (d.get("code") or "").upper())
    name, admin_name = (d.get("name") or "").strip(), (d.get("admin_name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    c = db()
    if len(name) < 3:
        return err("Enter the agency name.")
    if len(code) != 3:
        return err("The agency code must be exactly 3 letters, e.g. TRX.")
    if code == "SUP" or c.execute("SELECT 1 FROM agencies WHERE code=?", (code,)).fetchone():
        return err(f"The code {code} is already used. Choose another one.")
    if c.execute("SELECT 1 FROM agencies WHERE lower(name)=lower(?)", (name,)).fetchone():
        return err("This agency already exists.")
    if email and not EMAIL_RE.match(email):
        return err("Enter a valid email address, or leave it empty.")
    admin_code, pw = f"ADM-{code}", new_password()
    note, problem = deliver_account(email, admin_name or f"{name} admin", f"{name} admin", admin_code, pw, "Admin")
    if problem:
        return err(f"The email could not be sent ({problem}). Nothing was created.", 502)
    c.execute("INSERT INTO agencies VALUES (?,?,?,?)", (code, name, "Active", ""))
    c.execute("INSERT INTO admins (code, name, agency, pw_hash, email) VALUES (?,?,?,?,?)",
              (admin_code, admin_name or f"{name} admin", code, generate_password_hash(pw), email or None))
    c.execute("INSERT INTO issued_credentials VALUES (?,?,?)", (admin_code, pw, now()))
    c.commit()
    return jsonify({"ok": True, "code": admin_code, "password": pw,
                    "message": f"Agency {name} created with admin {admin_code}, temporary password {pw}. {note}"})


@app.post("/api/admin/admins")
def admin_create_admin():
    """Create the admin account for an existing agency that does not have one."""
    s, e = super_only()
    if e:
        return e
    d = request.get_json(silent=True) or {}
    agency = (d.get("agency") or "").upper()
    name, email = (d.get("name") or "").strip(), (d.get("email") or "").strip().lower()
    c = db()
    ag = c.execute("SELECT * FROM agencies WHERE code=?", (agency,)).fetchone()
    if not ag:
        return err("Choose the agency.")
    code = f"ADM-{agency}"
    if c.execute("SELECT 1 FROM admins WHERE code=?", (code,)).fetchone():
        return err(f"{ag['name']} already has an admin ({code}). Use Reset password to give it a new one.")
    if email and not EMAIL_RE.match(email):
        return err("Enter a valid email address, or leave it empty.")
    pw = new_password()
    note, problem = deliver_account(email, name or f"{ag['name']} admin", f"{ag['name']} admin", code, pw, "Admin")
    if problem:
        return err(f"The email could not be sent ({problem}). The admin was not created.", 502)
    c.execute("INSERT INTO admins (code, name, agency, pw_hash, email) VALUES (?,?,?,?,?)",
              (code, name or f"{ag['name']} admin", agency, generate_password_hash(pw), email or None))
    c.execute("INSERT INTO issued_credentials VALUES (?,?,?)", (code, pw, now()))
    c.commit()
    return jsonify({"ok": True, "code": code, "password": pw, "message": f"Admin {code} created, temporary password {pw}. {note}"})


@app.get("/api/admin/overview")
def admin_overview():
    s = auth("admin")
    if not s:
        return err("Sign in as an admin first.", 401)
    c, ag = db(), s["agency"]
    events = [dict(r) for r in c.execute("SELECT * FROM events" + (" WHERE agency=?" if ag else "") +
                                         " ORDER BY ts DESC LIMIT 100", (ag,) if ag else ())]
    out = {"ok": True, "drivers": driver_rows(ag), "events": events, "alerts": alert_rows(agency=ag),
           "live": [dict(r) for r in c.execute("SELECT * FROM live WHERE updated>?" + (" AND agency=?" if ag else "")
                                               + " ORDER BY sector, name", (now() - LIVE_SECONDS, *((ag,) if ag else ())))]}
    out["email_ready"] = email_ready()
    if not ag:
        out["segments"] = segment_rows()
        out["limit_changes"] = [dict(r) for r in c.execute("SELECT * FROM limit_changes ORDER BY ts DESC LIMIT 20")]
        issued = {r["code"]: r["password"] for r in c.execute("SELECT * FROM issued_credentials")}
        out["agencies"] = [dict(r, drivers=c.execute("SELECT COUNT(*) FROM drivers WHERE agency=?", (r["code"],)).fetchone()[0])
                           for r in c.execute("SELECT * FROM agencies")]
        out["stations"] = [dict(code=r["code"], name=r["name"], sector=r["sector"], verified=bool(r["verified"]),
                                email=r["email"], issued_password=issued.get(r["code"]))
                           for r in c.execute("SELECT * FROM stations ORDER BY sector, code")]
        out["admins"] = [dict(code=r["code"], name=r["name"], agency=r["agency"], email=r["email"],
                              issued_password=issued.get(r["code"])) for r in c.execute("SELECT * FROM admins")]
        out["towns"] = TOWNS
        out["events_total"] = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return jsonify(out)


# ---------------------------------------------------------------- vehicle (driver app or hardware unit)
def sector_mode(sector, agency):
    r = db().execute("SELECT mode FROM agency_policies WHERE sector=? AND agency=?", (sector, agency)).fetchone()
    return r["mode"] if r else "reduce"


@app.post("/api/driver/tick")
def driver_tick():
    """Vehicle heartbeat, about once a second, from the driver app or a hardware on-board unit.

    Body: speed (km/h), lat, lon, status, progress (%), source. The roadside station whose coverage zone contains
    the vehicle detects it (FR1) and sends the limit of its road segment (FR2). Outside every zone the vehicle
    keeps the last limit it received. Speed and position are sampled into the telemetry log (FR7), and a vehicle
    that stays over the limit raises an alert (FR10). Returns the segment, limit, regulation mode and signals."""
    s = auth("driver")
    if not s:
        return err("Sign in as a driver first.", 401)
    d = request.get_json(silent=True) or {}
    c, t = db(), now()
    speed, pos = max(0.0, num(d.get("speed"))), position(d)
    status, source = str(d.get("status") or "")[:40], str(d.get("source") or "")[:12]
    prev = c.execute("SELECT * FROM live WHERE driver_code=?", (s["code"],)).fetchone()
    fresh = bool(prev) and t - prev["updated"] < HOLD_SECONDS and \
        (prev["trip_from"], prev["trip_to"]) == (s["trip_from"], s["trip_to"])

    seg, dist = detect_segment(c, pos)
    coverage = seg is not None
    if seg is None:
        if pos is None and d.get("sector") in TOWNS:  # a unit without GPS that names its sector
            seg = get_segment(c, TOWN_CODES[d["sector"]])
        elif fresh and prev["segment"]:  # between zones: keep the last limit received
            seg = get_segment(c, prev["segment"])
        if seg is None:  # nothing received yet on this trip: the town the trip is closest to
            seg = get_segment(c, TOWN_CODES[s["trip_from"] if num(d.get("progress")) < 50 else s["trip_to"]])
    sector, lim = seg["sector"], seg["limit_kmh"]
    mode = sector_mode(sector, s["agency"])
    ev = dict(sector=sector, segment=seg["code"], driver_code=s["code"], driver_name=s["name"], agency=s["agency"],
              car_code=s["car_code"], speed=round(speed, 1), limit_kmh=lim)

    detected = coverage and not (fresh and prev["in_coverage"] and prev["segment"] == seg["code"])
    if detected:
        c.execute("INSERT INTO detections (ts, rsu, segment, sector, driver_code, agency, car_code, speed, lat, lon,"
                  " distance_km) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (t, seg["rsu"], seg["code"], sector, s["code"], s["agency"], s["car_code"], speed, *pos, round(dist, 2)))
        mqtt_publish(f"corridor/rsu/{seg['rsu'].lower()}/detections",
                     {"rsu": seg["rsu"], "segment": seg["code"], "car_code": s["car_code"], "driver_code": s["code"],
                      "speed_kmh": round(speed, 1), "distance_km": round(dist, 2), "ts": iso(t)})
        mqtt_publish(f"corridor/vehicles/{s['car_code']}/limit",
                     {"rsu": seg["rsu"], "segment": seg["code"], "name": seg["name"], "limit_kmh": lim, "mode": mode,
                      "ts": iso(t)})

    over = speed > lim + 0.5
    over_since = (prev["over_since"] if fresh and prev["over_since"] else t) if over else None
    if over and t - over_since >= SUSTAINED_SECONDS and not c.execute(
            "SELECT 1 FROM alerts WHERE driver_code=? AND kind='sustained_speeding' AND ts>=?", (s["code"], over_since)).fetchone():
        raise_alert(c, "high", "sustained_speeding",
                    f"Over the limit for {t - over_since:.0f} s: {speed:.0f} km/h in a {lim} km/h segment"
                    + (" (agency set to warn only)" if mode == "warn" else ", speed not coming down"), ev)

    log = not fresh or prev["logged"] is None or t - prev["logged"] >= TELEMETRY_SECONDS \
        or prev["status"] != status or prev["segment"] != seg["code"]
    lat, lon = pos or (None, None)
    if log:
        c.execute("INSERT INTO telemetry (ts, driver_code, agency, car_code, segment, sector, speed, limit_kmh, status,"
                  " lat, lon, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (t, s["code"], s["agency"], s["car_code"], seg["code"], sector, speed, lim, status, lat, lon, source))
    c.execute(database.upsert("live", "driver_code, name, agency, car_code, sector, segment, in_coverage, trip_from,"
                              " trip_to, speed, limit_kmh, status, progress, lat, lon, source, over_since, logged, updated",
                              "driver_code"),
              (s["code"], s["name"], s["agency"], s["car_code"], sector, seg["code"], int(coverage), s["trip_from"],
               s["trip_to"], speed, lim, status, num(d.get("progress")), lat, lon, source, over_since,
               t if log else prev["logged"], t))
    c.commit()
    if (prev["status"] if prev else None) != status:  # tell the vehicle unit what to do
        action = "reduce" if status in ("Reducing speed", "Held at limit") else "warn" if status.startswith("Over") or status == "Warning" else "release"
        mqtt_publish(f"corridor/vehicles/{s['car_code']}/command",
                     {"action": action, "target_kmh": lim, "rate_kmh_s": REDUCE_RATE_KMH_S, "segment": seg["code"],
                      "sector": sector, "driver_code": s["code"], "ts": iso()})
    sig = c.execute("SELECT * FROM signals WHERE sector=? AND created>? AND (target_type='all' OR "
                    "(target_type='agency' AND target=?) OR (target_type='driver' AND target=?)) ORDER BY created DESC LIMIT 20",
                    (sector, now() - 1800, s["agency"], s["code"])).fetchall()
    return jsonify({"ok": True, "sector": sector, "limit": lim, "mode": mode, "coverage": coverage, "detected": detected,
                    "distance_km": round(dist, 2) if coverage else None,
                    "segment": {k: seg[k] for k in ("code", "name", "sector", "rsu", "radius_km")},
                    "signals": [dict(r) for r in sig]})


@app.post("/api/driver/events")
def driver_event():
    """A warning or speed-reduction event from the vehicle (FR7, FR8). It is stored with its speed, location and
    time and published on corridor/speedevents. Body: type, speed_kmh, limit_kmh, speed_reduced, mode, lat, lon,
    segment, device_ts (ms), event_id. Resending the same event_id is harmless."""
    s = auth("driver")
    if not s:
        return err("Sign in as a driver first.", 401)
    d = request.get_json(silent=True) or {}
    c = db()
    speed, limit, pos = num(d.get("speed_kmh")), num(d.get("limit_kmh")), position(d)
    seg = get_segment(c, d.get("segment")) or detect_segment(c, pos)[0]
    payload = {"event_id": d.get("event_id") or str(uuid.uuid4()), "timestamp": iso(), "type": d.get("type") or "warning",
               "agency": s["agency_name"], "agency_code": s["agency"], "driver_code": s["code"], "driver_name": s["name"],
               "car_code": s["car_code"], "sector": seg["sector"] if seg else d.get("sector"),
               "segment": seg["code"] if seg else None, "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
               "speed_kmh": round(speed, 1), "limit_kmh": round(limit), "severity": severity(speed, limit),
               "speed_reduced": bool(d.get("speed_reduced")), "mode": d.get("mode"),
               "speed_source": d.get("speed_source"), "source": "server"}
    device_ts = num(d.get("device_ts"), 0)
    if 1e12 < device_ts < 1e13:  # milliseconds since 1970
        payload["device_time"] = iso(device_ts / 1000)
    store_event(c, dict(payload, device_ts=device_ts), d.get("speed_source") or "app")
    return jsonify({"ok": True, "topic": EVENTS_TOPIC, "published": mqtt_publish(EVENTS_TOPIC, payload), "payload": payload})


# ---------------------------------------------------------------- police
@app.get("/api/police/dashboard")
def police_dashboard():
    s = auth("police")
    if not s:
        return err("Sign in as a police station first.", 401)
    c, sector = db(), s["sector"]
    segs = segment_rows(sector)
    return jsonify({
        "ok": True, "sector": sector, "segments": segs, "alerts": alert_rows(sector=sector),
        "limit": next((g["limit_kmh"] for g in segs if g["code"] == TOWN_CODES.get(sector)), None),
        "live": [dict(r) for r in c.execute("SELECT * FROM live WHERE sector=? AND updated>?", (sector, now() - LIVE_SECONDS))],
        "events": [dict(r) for r in c.execute("SELECT * FROM events WHERE sector=? AND ts>? ORDER BY ts DESC LIMIT 100", (sector, now() - 86400))],
        "policies": {r["agency"]: r["mode"] for r in c.execute("SELECT * FROM agency_policies WHERE sector=?", (sector,))},
        "signals": [dict(r) for r in c.execute("SELECT * FROM signals WHERE sector=? ORDER BY created DESC LIMIT 15", (sector,))],
        "drivers": [dict(r) for r in c.execute("SELECT code, name, agency FROM drivers ORDER BY name")],
        "agencies": [dict(r) for r in c.execute("SELECT * FROM agencies")],
    })


@app.post("/api/police/limit")
def police_limit():
    """A police station sets the limit of a road segment in its own sector (the town's urban segment by default)."""
    s = auth("police")
    if not s:
        return err("Sign in as a police station first.", 401)
    d = request.get_json(silent=True) or {}
    lim = int(num(d.get("limit")))
    if not 10 <= lim <= 130:
        return err("Speed limit must be between 10 and 130 km/h.")
    c = db()
    seg = get_segment(c, d.get("segment") or TOWN_CODES.get(s["sector"]))
    if not seg or seg["sector"] != s["sector"]:
        return err("Choose a road segment in your sector.", 403)
    set_segment_limit(c, seg, lim, s["code"])
    return jsonify({"ok": True, "limit": lim, "segment": seg["code"], "name": seg["name"]})


@app.post("/api/police/policy")
def police_policy():
    s = auth("police")
    if not s:
        return err("Sign in as a police station first.", 401)
    d = request.get_json(silent=True) or {}
    if d.get("mode") not in ("warn", "reduce"):
        return err("Mode must be warn or reduce.")
    db().execute(database.upsert("agency_policies", "sector, agency, mode", "sector, agency"),
                 (s["sector"], (d.get("agency") or "").upper(), d["mode"]))
    db().commit()
    return jsonify({"ok": True})


@app.post("/api/police/signal")
def police_signal():
    s = auth("police")
    if not s:
        return err("Sign in as a police station first.", 401)
    d = request.get_json(silent=True) or {}
    ttype, target, msg = d.get("target_type"), (d.get("target") or "").upper(), (d.get("message") or "").strip()
    if ttype not in ("driver", "agency", "all") or not msg or (ttype != "all" and not target):
        return err("Choose who receives the signal and write a message.")
    db().execute("INSERT INTO signals (sector, station_code, station_name, target_type, target, message, created) VALUES (?,?,?,?,?,?,?)",
                 (s["sector"], s["code"], s["name"], ttype, target, msg[:200], now()))
    db().commit()
    mqtt_publish(f"corridor/signals/{slug(s['sector'])}", {"sector": s["sector"], "station": s["name"], "target_type": ttype,
                                                         "target": target, "message": msg[:200], "ts": iso()})
    return jsonify({"ok": True})


# ---------------------------------------------------------------- road segments, alerts and history
@app.post("/api/admin/segments/<code>")
def admin_segment(code):
    """FR12: the super admin sets the speed limit, and the station's coverage radius, of any road segment."""
    s = auth("admin")
    if not s:
        return err("Sign in as an admin first.", 401)
    if s["agency"]:
        return err("Only the super admin can change road segment limits.", 403)
    d = request.get_json(silent=True) or {}
    c = db()
    seg = get_segment(c, code)
    if not seg:
        return err("Road segment not found.", 404)
    lim, radius = int(num(d.get("limit_kmh"))), num(d.get("radius_km"), seg["radius_km"])
    if not 10 <= lim <= 130:
        return err("Speed limit must be between 10 and 130 km/h.")
    if not 0.5 <= radius <= 100:
        return err("The coverage radius must be between 0.5 and 100 km.")
    set_segment_limit(c, seg, lim, s["code"], round(radius, 1))
    return jsonify({"ok": True, "message": f"{seg['name']}: limit {lim} km/h, coverage {radius:g} km."})


@app.post("/api/alerts/<int:aid>/ack")
def alert_ack(aid):
    """Mark an alert as handled. Police: alerts in their sector. Admins: all, or their agency's."""
    s = auth("police", "admin")
    if not s:
        return err("Sign in first.", 401)
    c = db()
    a = c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    if s["role"] == "police":
        allowed = a is not None and a["sector"] == s["sector"]
    else:
        allowed = a is not None and (not s["agency"] or a["agency"] == s["agency"])
    if not allowed:
        return err("Alert not found.", 404)
    if not a["ack_ts"]:
        c.execute("UPDATE alerts SET ack_by=?, ack_ts=? WHERE id=?", (s["code"], now(), aid))
        c.commit()
    return jsonify({"ok": True})


def history_filters(s):
    """Read the history filters and check them against what the user may see: police only the segments of their
    sector, agency admins every segment but only their own vehicles. Returns (filters, error response)."""
    segs = segment_rows(s["sector"] if s["role"] == "police" else None)
    codes = [g["code"] for g in segs]
    code = (request.args.get("segment") or "").upper()
    if code and code not in codes:
        return None, err("That road segment is not in your area.", 403)
    t1 = min(max(num(request.args.get("to"), now()), 0), 4e9)
    t0 = min(max(num(request.args.get("from"), t1 - 7 * 86400), 0), 4e9)
    if t0 >= t1:
        return None, err("The start of the period must be before its end.")
    sev = request.args.get("severity")
    return dict(segs=segs, codes=codes, code=code, t0=t0, t1=t1, severity=sev if sev in ("low", "medium", "high") else None,
                agency=s["agency"] if s["role"] == "admin" else None), None


def history_where(f, every=False):
    """WHERE clause for the period, the selected segment (or every allowed one) and the agency restriction."""
    codes = (f["codes"] if every or not f["code"] else [f["code"]]) or [""]
    q, args = f"ts>=? AND ts<? AND segment IN ({','.join('?' * len(codes))})", [f["t0"], f["t1"], *codes]
    if f["agency"]:
        q, args = q + " AND agency=?", args + [f["agency"]]
    return q, args


@app.get("/api/history")
def history():
    """FR11: speed and violation history by road segment. Query: segment, from, to (Unix seconds), severity,
    tz (browser offset from UTC in minutes, for hourly/daily buckets). Always returns a summary of every allowed
    segment; with a segment, also its percentiles, a time series, its events and its limit changes."""
    s = auth("police", "admin")
    if not s:
        return err("Sign in as a police station or an admin first.", 401)
    f, e = history_filters(s)
    if e:
        return e
    c = db()
    blank = dict(samples=0, avg_speed=None, max_speed=None, over_pct=None, violations=0, low=0, medium=0, high=0,
                 regulations=0, alerts=0)
    summary = {g["code"]: dict(g, **blank) for g in f["segs"]}
    w, a = history_where(f, every=True)
    for r in c.execute(f"SELECT segment, COUNT(*) n, AVG(speed) avg, MAX(speed) mx,"
                       f" SUM(CASE WHEN speed > limit_kmh + 0.5 THEN 1 ELSE 0 END) n_over"
                       f" FROM telemetry WHERE {w} AND speed>=? GROUP BY segment", a + [MOVING_KMH]):
        summary[r["segment"]].update(samples=r["n"], avg_speed=round(r["avg"], 1), max_speed=round(r["mx"], 1),
                                     over_pct=round(100 * r["n_over"] / r["n"], 1))
    for r in c.execute(f"SELECT segment, kind, severity, COUNT(*) n FROM events WHERE {w} GROUP BY segment, kind, severity", a):
        row = summary[r["segment"]]
        if r["kind"] == "speed_reduced":
            row["regulations"] += r["n"]
        else:
            row["violations"] += r["n"]
            if r["severity"] in ("low", "medium", "high"):
                row[r["severity"]] += r["n"]
    for r in c.execute(f"SELECT segment, COUNT(*) n FROM alerts WHERE {w} GROUP BY segment", a):
        summary[r["segment"]]["alerts"] = r["n"]

    selected = None
    if f["code"]:
        w, a = history_where(f)
        speeds = [r[0] for r in c.execute(f"SELECT speed FROM telemetry WHERE {w} AND speed>=? ORDER BY speed", a + [MOVING_KMH])]
        bucket = 3600 if f["t1"] - f["t0"] <= 3 * 86400 else 86400
        shift = int(num(request.args.get("tz"))) * 60
        series, b = {}, database.floor_int("(ts + ?) / ?")
        for r in c.execute(f"SELECT {b} b, COUNT(*) n, AVG(speed) avg, MAX(speed) mx, MAX(limit_kmh) lim"
                           f" FROM telemetry WHERE {w} AND speed>=? GROUP BY b", [shift, bucket, *a, MOVING_KMH]):
            series[r["b"]] = dict(t=r["b"] * bucket - shift, samples=r["n"], avg=round(r["avg"], 1), max=round(r["mx"], 1),
                                  limit=r["lim"], violations=0)
        for r in c.execute(f"SELECT {b} b, COUNT(*) n FROM events WHERE {w} AND kind!='speed_reduced'"
                           f" GROUP BY b", [shift, bucket, *a]):
            series.setdefault(r["b"], dict(t=r["b"] * bucket - shift, samples=0, avg=None, max=None, limit=None,
                                           violations=0))["violations"] = r["n"]
        sev = " AND severity=?" if f["severity"] else ""
        events = [dict(r) for r in c.execute(
            f"SELECT event_id, ts, received, agency, driver_code, driver_name, car_code, sector, segment, lat, lon, speed,"
            f" limit_kmh, severity, speed_reduced, kind, source FROM events WHERE {w}{sev} ORDER BY ts DESC LIMIT 500",
            a + ([f["severity"]] if f["severity"] else []))]
        changes = [dict(r) for r in c.execute("SELECT * FROM limit_changes WHERE segment=? AND ts>=? AND ts<? ORDER BY ts DESC",
                                              (f["code"], f["t0"], f["t1"]))]
        selected = dict(summary[f["code"]], p85_speed=percentile(speeds, 85), median_speed=percentile(speeds, 50),
                        bucket=bucket, series=[series[k] for k in sorted(series)], events=events, changes=changes)
    return jsonify({"ok": True, "from": f["t0"], "to": f["t1"], "segments": list(summary.values()), "selected": selected})


@app.get("/api/history/export")
def history_export():
    """CSV of the data behind the history view: data=events (default) or data=speeds (the telemetry samples)."""
    s = auth("police", "admin")
    if not s:
        return err("Sign in as a police station or an admin first.", 401)
    f, e = history_filters(s)
    if e:
        return e
    speeds = request.args.get("data") == "speeds"
    table = "telemetry" if speeds else "events"
    cols = (["ts", "segment", "sector", "driver_code", "agency", "car_code", "speed", "limit_kmh", "status", "lat", "lon", "source"]
            if speeds else
            ["ts", "received", "segment", "sector", "driver_code", "driver_name", "agency", "car_code", "speed", "limit_kmh",
             "severity", "kind", "speed_reduced", "lat", "lon", "source", "event_id"])
    w, a = history_where(f)
    if f["severity"] and not speeds:
        w, a = w + " AND severity=?", a + [f["severity"]]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([{"ts": "time_utc", "received": "received_utc"}.get(k, k) for k in cols])
    for r in db().execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {w} ORDER BY ts", a):
        writer.writerow([iso(v) if k in ("ts", "received") and v else v for k, v in zip(cols, r)])
    shift = int(num(request.args.get("tz"))) * 60  # name the file with the user's own dates
    first, last = (datetime.fromtimestamp(t + shift, tz=timezone.utc) for t in (f["t0"], f["t1"] - 1))
    name = f"{'speeds' if speeds else 'events'}-{(f['code'] or 'all-segments').lower()}-{first:%Y%m%d}-{last:%Y%m%d}.csv"
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- start
def lan_ip():
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except OSError:
        return "your-computer-ip"


def open_browser(url):
    """Open the app in Google Chrome if it is installed, otherwise in the default browser."""
    import shutil
    import subprocess
    import webbrowser
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome") or "", shutil.which("chrome") or "",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                subprocess.Popen([path, url])
                return
            except OSError:
                pass
    webbrowser.open(url)


init_db()
start_mqtt()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    lan = "--lan" in sys.argv
    ssl = None
    if os.environ.get("SSL_CERT") and os.environ.get("SSL_KEY"):
        ssl = (os.environ["SSL_CERT"], os.environ["SSL_KEY"])
    elif lan:
        ssl = "adhoc"
    if lan:  # links in emails must reach phones, not "localhost"
        os.environ.setdefault("APP_URL", f"https://{lan_ip()}:{port}")
    print("\n  Road speed regulation server")
    print(f"  This computer:   {'https' if ssl else 'http'}://localhost:{port}")
    if lan:
        print(f"  Phones on Wi-Fi: https://{lan_ip()}:{port}   (accept the certificate warning once)")
    print("  Email: " + ("Brevo API" if os.environ.get("BREVO_API_KEY") else "SMTP " + os.environ["SMTP_HOST"]
                         if os.environ.get("SMTP_HOST") else "NOT configured - emails are printed here"))
    if os.environ.get("APP_URL"):
        print(f"  Links in emails: {os.environ['APP_URL']}")
    print(f"  MQTT:  {'connected to ' + os.environ['MQTT_HOST'] if _mqtt else 'off'}")
    print(f"  Database: {database.describe()}")
    print("  Stop with Ctrl+C\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, open_browser, args=(f"{'https' if ssl else 'http'}://127.0.0.1:{port}",)).start()
    app.run(host="0.0.0.0" if lan else "127.0.0.1", port=port, debug=False, threaded=True, ssl_context=ssl)
