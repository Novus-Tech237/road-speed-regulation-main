"""Verification of the functional requirements FR1 to FR12 (Table 3.1).

Run from the project folder:
    python -m unittest tests.test_requirements -v

Each test case is named after the requirement it verifies. The server runs against a temporary database,
with email printed to the console and MQTT replaced by a recorder, so nothing is sent anywhere.
The temporary database is SQLite. To run the same tests on PostgreSQL, set RSR_TEST_DATABASE_URL to an EMPTY
database (e.g. a throwaway Neon branch, never the one in use): the tests fill it with their own data.
FR4 to FR6 run in the on-board unit (the driver page); test_fr04_fr05_fr06_onboard_regulation drives it in a
real browser when Playwright and Chrome are available, and is skipped otherwise.
DriverAccount checks the account flow: an admin registers a driver, the driver gets the details by email, signs in
and replaces the temporary password; the browser test also follows the sign-in link from that email.
"""
import contextlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

TMP = tempfile.mkdtemp(prefix="rsr-test-")
os.environ.update(RSR_DB=os.path.join(TMP, "test.db"), RSR_CRED_FILE=os.path.join(TMP, "credentials.txt"),
                  DATABASE_URL=os.environ.get("RSR_TEST_DATABASE_URL", ""),
                  SMTP_HOST="", BREVO_API_KEY="", MQTT_HOST="", RSR_FORCE_HTTPS="")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as server  # noqa: E402

DOUALA = (4.0511, 9.7679)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)
OUTSIDE = (6.9, 13.6)  # far from every roadside station


class Recorder:
    """Stands in for the MQTT client and keeps every message published."""

    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.messages.append((topic, json.loads(payload), retain))

    def on(self, topic):
        return [p for t, p, _r in self.messages if t == topic]


def credentials():
    creds = {}
    with open(os.environ["RSR_CRED_FILE"], encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[1].startswith(("PS-", "ADM-")):
                creds[parts[1]] = parts[2]
    return creds


class Api:
    def __init__(self):
        self.client = server.app.test_client()

    def call(self, method, url, token=None, body=None):
        r = self.client.open(url, method=method, json=body, headers={"Authorization": f"Bearer {token}"} if token else {})
        return r.status_code, (r.get_json() if r.is_json else r.get_data(as_text=True))

    def login(self, role, code, password, **extra):
        status, data = self.call("POST", "/api/login", body=dict(role=role, code=code, password=password, **extra))
        assert status == 200, data
        return data["token"]


class Requirements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api, creds = Api(), credentials()
        cls.mqtt = server._mqtt = Recorder()
        cls.admin = cls.api.login("admin", "ADM-SUPER", creds["ADM-SUPER"])
        cls.agency_admin = cls.api.login("admin", "ADM-GEV", creds["ADM-GEV"])
        cls.police = cls.api.login("police", "PS-DLA-01", creds["PS-DLA-01"])
        cls.police_yde = cls.api.login("police", "PS-YDE-01", creds["PS-YDE-01"])
        cls.driver_code, cls.driver = cls.new_driver("Jean Test", "jean.test@example.com", "GEV-BUS-07")

    @classmethod
    def new_driver(cls, name, email, car):
        with contextlib.redirect_stdout(io.StringIO()):  # the welcome email is printed when SMTP is not set up
            status, data = cls.api.call("POST", "/api/admin/drivers", cls.admin, dict(name=name, email=email, agency="GEV", car_code=car))
        assert status == 200, data
        _s, overview = cls.api.call("GET", "/api/admin/overview", cls.admin)
        d = next(x for x in overview["drivers"] if x["email"] == email)
        token = cls.api.login("driver", d["code"], d["issued_password"], agency="GEV", car_code=car, name=name,
                              trip_from="Douala", trip_to="Yaoundé")
        return d["code"], token

    def tick(self, pos, speed=40, status="Within limit", token=None, progress=0):
        body = dict(speed=speed, status=status, progress=progress, source="test")
        if pos:
            body.update(lat=pos[0], lon=pos[1])
        status_code, data = self.api.call("POST", "/api/driver/tick", token or self.driver, body)
        self.assertEqual(status_code, 200, data)
        return data

    def segment(self, code):
        return next(g for g in self.api.call("GET", "/api/meta")[1]["segments"] if g["code"] == code)

    def db(self):
        conn = server.connect()
        self.addCleanup(conn.close)
        return conn

    # ------------------------------------------------------------------ FR1
    def test_fr01_roadside_station_detects_vehicle_in_coverage_zone(self):
        code, token = self.new_driver("Fr One", "fr1@example.com", "GEV-BUS-11")
        first = self.tick(DOUALA, token=token)
        self.assertTrue(first["coverage"])
        self.assertTrue(first["detected"], "entering a zone is a detection")
        self.assertEqual((first["segment"]["code"], first["segment"]["rsu"]), ("DLA", "RSU-DLA"))
        self.assertFalse(self.tick(DOUALA, token=token)["detected"], "staying in the zone is not a new detection")

        road = self.segment("DLA-EDE")
        on_road = self.tick((road["lat"], road["lon"]), token=token)
        self.assertEqual(on_road["segment"]["code"], "DLA-EDE")
        self.assertTrue(on_road["detected"])

        away = self.tick(OUTSIDE, token=token)
        self.assertFalse(away["coverage"], "no station covers this position")
        self.assertFalse(away["detected"])
        self.assertEqual(away["segment"]["code"], "DLA-EDE", "outside every zone the vehicle keeps the last limit")
        rows = self.db().execute("SELECT rsu FROM detections WHERE driver_code=? ORDER BY id", (code,)).fetchall()
        self.assertEqual([r["rsu"] for r in rows], ["RSU-DLA", "RSU-DLA-EDE"])

    # ------------------------------------------------------------------ FR2
    def test_fr02_station_determines_and_transmits_limit(self):
        _code, token = self.new_driver("Fr Two", "fr2@example.com", "GEV-BUS-12")
        data = self.tick(DOUALA, token=token)
        self.assertEqual(data["limit"], self.segment("DLA")["limit_kmh"])
        sent = self.mqtt.on("corridor/vehicles/GEV-BUS-12/limit")
        self.assertTrue(sent, "the limit is sent to the vehicle when the station detects it")
        self.assertEqual((sent[-1]["segment"], sent[-1]["limit_kmh"]), ("DLA", data["limit"]))

    # ------------------------------------------------------------------ FR3
    def test_fr03_onboard_unit_receives_limit_and_reports_speed(self):
        data = self.tick(DOUALA, speed=47.5)
        self.assertIsInstance(data["limit"], int)
        self.assertIn(data["mode"], ("reduce", "warn"))
        live = self.db().execute("SELECT speed, limit_kmh FROM live WHERE driver_code=?", (self.driver_code,)).fetchone()
        self.assertEqual((live["speed"], live["limit_kmh"]), (47.5, data["limit"]))

    # ------------------------------------------------------------------ FR4, FR5, FR6 (server side)
    def test_fr04_comparison_classifies_speed_against_limit(self):
        self.assertEqual(server.severity(50, 50), "none")
        self.assertEqual(server.severity(55, 50), "low")
        self.assertEqual(server.severity(62, 50), "medium")
        self.assertEqual(server.severity(63, 50), "high")

    def test_fr05_fr06_warning_then_progressive_reduction_commands(self):
        _code, token = self.new_driver("Fr Six", "fr6@example.com", "GEV-BUS-16")
        topic = "corridor/vehicles/GEV-BUS-16/command"
        self.tick(DOUALA, speed=40, token=token)
        self.tick(DOUALA, speed=58, status="Warning", token=token)
        self.tick(DOUALA, speed=57, status="Reducing speed", token=token)
        actions = [p["action"] for p in self.mqtt.on(topic)]
        self.assertEqual(actions[-2:], ["warn", "reduce"], "warning first, regulation after it")
        reduce = self.mqtt.on(topic)[-1]
        self.assertEqual(reduce["target_kmh"], self.segment("DLA")["limit_kmh"])
        self.assertGreater(reduce["rate_kmh_s"], 0, "the reduction is progressive, not sudden")

    # ------------------------------------------------------------------ FR7
    def test_fr07_logs_speed_location_time_and_events(self):
        code, token = self.new_driver("Fr Seven", "fr7@example.com", "GEV-BUS-17")
        self.tick(DOUALA, speed=44, token=token)
        sample = self.db().execute("SELECT * FROM telemetry WHERE driver_code=?", (code,)).fetchone()
        self.assertEqual((sample["speed"], sample["lat"], sample["lon"], sample["segment"]), (44, *DOUALA, "DLA"))
        self.assertAlmostEqual(sample["ts"], time.time(), delta=5)

        happened = int(time.time() * 1000) - 3000
        status, data = self.api.call("POST", "/api/driver/events", token, dict(
            type="warning", speed_kmh=61, limit_kmh=50, speed_reduced=False, lat=DOUALA[0], lon=DOUALA[1],
            segment="DLA", device_ts=happened, event_id="fr7-event"))
        self.assertEqual(status, 200, data)
        ev = self.db().execute("SELECT * FROM events WHERE event_id='fr7-event'").fetchone()
        self.assertEqual((ev["speed"], ev["lat"], ev["lon"], ev["segment"], ev["severity"]), (61, *DOUALA, "DLA", "medium"))
        self.assertAlmostEqual(ev["ts"], happened / 1000, delta=0.01, msg="the time it happened on the vehicle")

    # ------------------------------------------------------------------ FR8
    def test_fr08_events_reach_server_in_near_real_time(self):
        before = len(self.mqtt.on(server.EVENTS_TOPIC))
        sent = time.time()
        body = dict(type="warning", speed_kmh=58, limit_kmh=50, lat=DOUALA[0], lon=DOUALA[1], segment="DLA",
                    device_ts=int(sent * 1000), event_id="fr8-event")
        status, data = self.api.call("POST", "/api/driver/events", self.driver, body)
        self.assertEqual(status, 200, data)
        ev = self.db().execute("SELECT ts, received FROM events WHERE event_id='fr8-event'").fetchone()
        self.assertLess(ev["received"] - sent, 1.0, "stored within a second of being sent")
        self.assertEqual(len(self.mqtt.on(server.EVENTS_TOPIC)), before + 1, "published on corridor/speedevents")
        self.api.call("POST", "/api/driver/events", self.driver, body)  # a vehicle resending after a lost reply
        n = self.db().execute("SELECT COUNT(*) FROM events WHERE event_id='fr8-event'").fetchone()[0]
        self.assertEqual(n, 1, "a resent event is stored once")

    # ------------------------------------------------------------------ FR9
    def test_fr09_dashboards_show_live_events_to_authorized_users(self):
        self.tick(DOUALA, speed=52)
        self.api.call("POST", "/api/driver/events", self.driver, dict(type="warning", speed_kmh=57, limit_kmh=50,
                                                                    segment="DLA", event_id="fr9-event"))
        status, police = self.api.call("GET", "/api/police/dashboard", self.police)
        self.assertEqual(status, 200)
        self.assertIn(self.driver_code, [v["driver_code"] for v in police["live"]])
        self.assertIn("fr9-event", [e["event_id"] for e in police["events"]])
        _s, admin = self.api.call("GET", "/api/admin/overview", self.admin)
        self.assertIn(self.driver_code, [v["driver_code"] for v in admin["live"]])
        self.assertEqual(self.api.call("GET", "/api/police/dashboard", self.driver)[0], 401, "drivers are not authorized")
        self.assertEqual(self.api.call("GET", "/api/police/dashboard")[0], 401)

    # ------------------------------------------------------------------ FR10
    def test_fr10_alerts_for_significant_violations_and_high_risk(self):
        code, token = self.new_driver("Fr Ten", "fr10@example.com", "GEV-BUS-20")

        def event(speed, n):
            self.api.call("POST", "/api/driver/events", token, dict(type="warning", speed_kmh=speed, limit_kmh=50,
                                                                  segment="DLA", event_id=f"fr10-{n}"))

        def alerts():
            return {a["kind"]: a for a in self.api.call("GET", "/api/police/dashboard", self.police)[1]["alerts"]
                    if a["driver_code"] == code}

        event(54, 1)
        self.assertEqual(alerts(), {}, "a low-severity violation is not an alert")
        event(80, 2)
        self.assertEqual(alerts()["extreme_speed"]["level"], "critical")
        event(55, 3)
        self.assertEqual(alerts()["repeat_offender"]["level"], "medium", "third violation within 15 min")

        conn = self.db()  # the vehicle has been over the limit for 31 s
        self.tick(DOUALA, speed=70, status="Over limit (warn only)", token=token)
        conn.execute("UPDATE live SET over_since=? WHERE driver_code=?", (time.time() - 31, code))
        conn.commit()
        self.tick(DOUALA, speed=70, status="Over limit (warn only)", token=token)
        self.assertEqual(alerts()["sustained_speeding"]["level"], "high")
        self.assertTrue(self.mqtt.on("corridor/alerts/douala"), "alerts are published")

        alert_id = alerts()["extreme_speed"]["id"]
        self.assertEqual(self.api.call("POST", f"/api/alerts/{alert_id}/ack", self.police_yde)[0], 404, "other sector")
        self.assertEqual(self.api.call("POST", f"/api/alerts/{alert_id}/ack", self.police)[0], 200)
        self.assertEqual(alerts()["extreme_speed"]["ack_by"], "PS-DLA-01")

    # ------------------------------------------------------------------ FR11
    def test_fr11_history_by_road_segment(self):
        for speed in (40, 45, 50, 55):
            self.tick(DOUALA, speed=speed, status=f"Sample {speed}")
        self.api.call("POST", "/api/driver/events", self.driver, dict(type="warning", speed_kmh=66, limit_kmh=50,
                                                                    segment="DLA", event_id="fr11-event"))
        t = time.time()
        status, data = self.api.call("GET", f"/api/history?segment=DLA&from={t - 3600}&to={t + 60}", self.police)
        self.assertEqual(status, 200, data)
        sel = data["selected"]
        self.assertGreaterEqual(sel["samples"], 4)
        self.assertGreaterEqual(sel["violations"], 1)
        self.assertGreaterEqual(sel["high"], 1)
        self.assertIsNotNone(sel["p85_speed"])
        self.assertTrue(sel["series"])
        self.assertIn("fr11-event", [e["event_id"] for e in sel["events"]])
        self.assertEqual({g["sector"] for g in data["segments"]}, {"Douala"}, "police see their own sector")

        _s, high = self.api.call("GET", f"/api/history?segment=DLA&severity=high&from={t - 3600}&to={t + 60}", self.police)
        self.assertEqual({e["severity"] for e in high["selected"]["events"]}, {"high"})
        self.assertEqual(self.api.call("GET", "/api/history?segment=DLA", self.police_yde)[0], 403, "another sector")
        status, csv_text = self.api.call("GET", f"/api/history/export?segment=DLA&from={t - 3600}&to={t + 60}", self.police)
        self.assertEqual(status, 200)
        self.assertTrue(csv_text.startswith("time_utc,received_utc,segment"))
        self.assertIn("fr11-event", csv_text)

    # ------------------------------------------------------------------ FR12
    def test_fr12_administrator_configures_limit_per_segment(self):
        road = self.segment("EDE-YDE")
        status, data = self.api.call("POST", "/api/admin/segments/EDE-YDE", self.admin, dict(limit_kmh=70))
        self.assertEqual(status, 200, data)
        self.assertEqual(self.tick((road["lat"], road["lon"]))["limit"], 70, "vehicles receive the new limit")
        self.assertEqual(self.mqtt.on("corridor/limits/ede-yde")[-1]["limit_kmh"], 70)
        change = self.db().execute("SELECT * FROM limit_changes WHERE segment='EDE-YDE' ORDER BY id DESC").fetchone()
        self.assertEqual((change["old_limit"], change["new_limit"], change["set_by"]), (road["limit_kmh"], 70, "ADM-SUPER"))

        self.assertEqual(self.api.call("POST", "/api/admin/segments/EDE-YDE", self.agency_admin, dict(limit_kmh=90))[0], 403)
        self.assertEqual(self.api.call("POST", "/api/admin/segments/EDE-YDE", self.admin, dict(limit_kmh=200))[0], 400)
        self.assertEqual(self.api.call("POST", "/api/admin/segments/EDE-YDE", self.driver, dict(limit_kmh=60))[0], 401)
        self.assertEqual(self.api.call("POST", "/api/police/limit", self.police, dict(segment="EDE-YDE", limit=60))[0], 403,
                         "police set limits only in their own sector")
        self.assertEqual(self.api.call("POST", "/api/police/limit", self.police, dict(segment="DLA-EDE", limit=90))[0], 200)


def register_driver(api, admin, name, email, car, agency="GEV"):
    """An admin registers a driver. Returns the response and the welcome email as printed (SMTP is not set up)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status, data = api.call("POST", "/api/admin/drivers", admin, dict(name=name, email=email, agency=agency, car_code=car))
    assert status == 200, data
    return data, out.getvalue()


def email_field(email, label):
    m = re.search(rf"^\s*{label}:\s*(\S.*?)\s*$", email, re.M)
    return m and m.group(1)


class DriverAccount(unittest.TestCase):
    """An admin creates a driver account, the driver receives everything needed in the email, signs in, must
    replace the temporary password, and then signs in with their own."""

    @classmethod
    def setUpClass(cls):
        cls.api = Api()
        cls.admin = cls.api.login("admin", "ADM-SUPER", credentials()["ADM-SUPER"])

    def setUp(self):
        self.addCleanup(server.FAILS.clear)  # the wrong sign-ins tried here must not lock the test client's address

    def driver_login(self, **fields):
        body = dict(role="driver", trip_from="Douala", trip_to="Yaoundé", **fields)
        return self.api.call("POST", "/api/login", body=body)

    def test_admin_creates_driver_who_signs_in_from_the_email(self):
        data, email = register_driver(self.api, self.admin, "Aïcha Bello", "aicha.flow@example.com", "GEV-BUS-41")
        self.assertEqual(data["delivered_via"], "console")
        self.assertFalse(data["email_failed"])
        code, pw = data["code"], data["password"]
        # The email carries every detail the driver sign-in asks for, and a link that fills them in.
        self.assertIn("To: aicha.flow@example.com", email)
        self.assertEqual(email_field(email, "Driver code"), code)
        self.assertEqual(email_field(email, "Agency"), "General Express Voyages")
        self.assertEqual(email_field(email, "Car code"), "GEV-BUS-41")
        self.assertEqual(email_field(email, "Name"), "Aïcha Bello")
        self.assertEqual(email_field(email, "Temporary password"), pw)
        link = re.search(r"^(https?://\S+/#signin\?\S+)$", email, re.M).group(1)
        self.assertEqual(link, data["sign_in_url"])
        q = parse_qs(urlsplit(link).fragment.split("?", 1)[1])
        self.assertEqual({k: v[0] for k, v in q.items()},
                         dict(role="driver", code=code, agency="GEV", car="GEV-BUS-41", name="Aïcha Bello"))

        self.assertTrue(set(pw) <= set(server.PASSWORD_CHARS), f"no look-alike characters to mistype: {pw}")
        status, r = self.driver_login(code=code, agency="GEV", car_code="GEV-BUS-14", name="Aïcha Bello", password=pw)
        self.assertEqual(status, 401)
        self.assertIn("the car code is not the one on your account", r["error"], "the driver is told what to fix")
        status, r = self.driver_login(code=code, agency="GEV", car_code="GEV-BUS-41", name="Aïcha Bello", password=pw[:-1] + "x")
        self.assertIn("do not match our records", r["error"], "nothing is revealed without the right password")
        status, r = self.driver_login(code=code.lower(), agency="GEV", car_code="gev-bus-41", name="  aicha   BELLO ",
                                      password=f" {pw}\n")  # copied from the email with the spaces around it
        self.assertEqual(status, 200, r)
        self.assertTrue(r["must_change_password"], "a temporary password must be replaced")
        token = r["token"]
        self.assertTrue(self.api.call("GET", "/api/session", token)[1]["must_change_password"], "still asked after a reload")
        self.assertEqual(self.api.call("POST", "/api/driver/tick", token, dict(speed=30, lat=4.0511, lon=9.7679))[0], 200)

        status, r = self.api.call("POST", "/api/password", token, dict(old=pw, new="my-own-pass", new2="my-own-pass"))
        self.assertEqual(status, 200, r)
        self.assertFalse(self.api.call("GET", "/api/session", token)[1]["must_change_password"])
        self.assertEqual(self.driver_login(code=code, agency="GEV", car_code="GEV-BUS-41", name="Aïcha Bello", password=pw)[0], 401,
                         "the temporary password no longer works")
        status, r = self.driver_login(code=code, agency="GEV", car_code="GEV-BUS-41", name="Aïcha Bello", password="my-own-pass")
        self.assertEqual(status, 200, r)
        self.assertFalse(r["must_change_password"])
        self.assertEqual(self.driver_login(code=code, agency="GEV", car_code="GEV-BUS-99", name="Aïcha Bello",
                                           password="my-own-pass")[0], 401, "the car code must match")
        self.assertEqual(self.driver_login(code=code, agency="GEV", car_code="GEV-BUS-41", name="Someone Else",
                                           password="my-own-pass")[0], 401, "the name must match")

    def test_driver_is_kept_when_the_email_cannot_be_sent(self):
        def refuse(*_a, **_k):
            raise RuntimeError("could not reach the mail server")
        original, server.send_driver_code = server.send_driver_code, refuse
        self.addCleanup(setattr, server, "send_driver_code", original)
        status, data = self.api.call("POST", "/api/admin/drivers", self.admin,
                                     dict(name="Mail Down", email="mail.down@example.com", agency="FNX", car_code="FNX-BUS-02"))
        self.assertEqual(status, 200, data)
        self.assertTrue(data["email_failed"])
        self.assertIn("could not reach the mail server", data["message"])
        status, r = self.driver_login(code=data["code"], agency="FNX", car_code="FNX-BUS-02", name="Mail Down", password=data["password"])
        self.assertEqual(status, 200, "the admin can hand the details over and the driver signs in")

    def test_admin_reset_emails_a_new_temporary_password(self):
        data, _email = register_driver(self.api, self.admin, "Reset Flow", "reset.flow@example.com", "GEV-BUS-42")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status, r = self.api.call("POST", f"/api/admin/accounts/{data['code']}/reset", self.admin)
        self.assertEqual(status, 200, r)
        new_pw = email_field(out.getvalue(), "Temporary password")
        self.assertIn(new_pw, r["message"], "without email the admin is shown the new password")
        self.assertEqual(self.driver_login(code=data["code"], agency="GEV", car_code="GEV-BUS-42", name="Reset Flow",
                                           password=data["password"])[0], 401)
        status, r = self.driver_login(code=data["code"], agency="GEV", car_code="GEV-BUS-42", name="Reset Flow", password=new_pw)
        self.assertEqual(status, 200, r)
        self.assertTrue(r["must_change_password"])


@unittest.skipUnless(os.environ.get("RSR_BROWSER_TESTS", "1") == "1", "browser tests disabled")
class OnboardUnit(unittest.TestCase):
    """FR4, FR5 and FR6 run in the driver page, so they are checked in a real browser."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright is not installed (pip install playwright)")
        from werkzeug.serving import make_server
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}/"
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="chrome")
        except Exception:  # noqa: BLE001
            try:
                cls.browser = cls.pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                cls.pw.stop()
                cls.httpd.shutdown()
                raise unittest.SkipTest(f"No browser for Playwright: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def test_fr04_fr05_fr06_onboard_regulation(self):
        api = Api()
        admin = api.login("admin", "ADM-SUPER", credentials()["ADM-SUPER"])
        with contextlib.redirect_stdout(io.StringIO()):
            api.call("POST", "/api/admin/drivers", admin, dict(name="Browser Test", email="browser@example.com", agency="GEV", car_code="GEV-BUS-30"))
        d = next(x for x in api.call("GET", "/api/admin/overview", admin)[1]["drivers"] if x["email"] == "browser@example.com")
        page = self.browser.new_page()
        page.goto(self.url)
        page.wait_for_function("document.querySelector('#modePill').textContent === 'Connected'")
        page.click("#topnav .nav-signin")
        page.click("#rt-driver")
        page.fill("#dlCode", d["code"])
        page.select_option("#dlAgency", "GEV")
        page.fill("#dlCar", "GEV-BUS-30")
        page.fill("#dlName", "Browser Test")
        page.select_option("#dlFrom", "Douala")
        page.select_option("#dlTo", "Yaoundé")
        page.fill("#dlPw", d["issued_password"])
        page.click("#formDriverLogin button[type=submit]")
        page.wait_for_selector("#dlgPassword[open]")  # temporary password: a new one must be chosen first
        page.fill("#pwNew", "browser-pass-1")
        page.fill("#pwNew2", "browser-pass-1")
        page.click("#formPassword button[type=submit]")
        page.wait_for_selector("#dlgPassword:not([open])", state="attached")
        page.check("input[name=src][value=test]", force=True)
        page.wait_for_function("document.querySelector('#signNum').textContent !== '--'")
        limit = int(page.text_content("#signNum"))
        page.evaluate("const a = document.querySelector('#accel'); a.value = 100; a.dispatchEvent(new Event('input'))")

        state = "document.querySelector('#warnLabel').classList"
        page.wait_for_function(f"{state}.contains('warn')", timeout=30000)  # FR4 + FR5: over the limit -> warning
        warned_at, speed_at_warning = time.time(), float(page.text_content("#gValue"))
        self.assertGreater(speed_at_warning, limit)
        page.wait_for_function(f"{state}.contains('reduce')", timeout=10000)  # FR6: still over -> regulation
        self.assertAlmostEqual(time.time() - warned_at, 2, delta=0.8, msg="regulation starts after the 2 s warning")
        readings = []
        for _ in range(6):
            readings.append(float(page.text_content("#gValue")))
            page.wait_for_timeout(500)
        self.assertTrue(all(b <= a for a, b in zip(readings, readings[1:])), f"speed only comes down: {readings}")
        self.assertTrue(all(a - b <= 4 for a, b in zip(readings, readings[1:])), f"no sudden braking: {readings}")
        page.wait_for_function(f"{state}.contains('held')", timeout=20000)
        self.assertLessEqual(float(page.text_content("#gValue")), limit + 0.5, "held at the limit")
        page.close()

    def test_driver_opens_the_email_link_and_reaches_the_dashboard(self):
        api = Api()
        admin = api.login("admin", "ADM-SUPER", credentials()["ADM-SUPER"])
        data, email = register_driver(api, admin, "Link Driver", "link.driver@example.com", "GEV-BUS-43")
        link = re.search(r"^(https?://\S+/#signin\?\S+)$", email, re.M).group(1)
        page = self.browser.new_page()
        page.goto(self.url + link[link.index("#"):])
        page.wait_for_selector("#formDriverLogin:not(.hidden)")
        self.assertEqual(page.input_value("#dlCode"), data["code"])
        self.assertEqual(page.input_value("#dlAgency"), "GEV")
        self.assertEqual(page.input_value("#dlCar"), "GEV-BUS-43")
        self.assertEqual(page.input_value("#dlName"), "Link Driver")
        self.assertNotIn("#signin", page.url, "the details are removed from the address bar")
        page.fill("#dlPw", data["password"])
        page.click("#formDriverLogin button[type=submit]")

        page.wait_for_selector("#dlgPassword[open]")
        self.assertFalse(page.is_visible("#pwClose"), "a temporary password cannot be kept")
        self.assertFalse(page.is_visible("#pwOld"), "the password just typed is used as the current one")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        self.assertTrue(page.is_visible("#dlgPassword"), "Escape does not skip the change")
        page.fill("#pwNew", "link-pass-1")
        page.fill("#pwNew2", "link-pass-1")
        page.click("#formPassword button[type=submit]")
        page.wait_for_selector("#dlgPassword:not([open])", state="attached")
        self.assertTrue(page.is_visible("#view-driver"))
        self.assertEqual(page.text_content("#dvName"), "Link Driver")
        page.reload()
        page.wait_for_selector("#view-driver:not(.hidden)")
        page.wait_for_timeout(300)
        self.assertFalse(page.is_visible("#dlgPassword"), "not asked again once changed")
        page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
