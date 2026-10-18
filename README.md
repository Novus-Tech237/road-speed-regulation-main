# Road speed regulation — deployment and use

## 1. Deploy online for free (Alwaysdata)
This hosts the app at an HTTPS address, so you do not need to run a server on your computer.
The free Alwaysdata plan includes a persistent 1 GB disk, which keeps the SQLite database
between site restarts. It is suitable for a small demo; it has limited CPU and memory.
To keep the data in a Neon PostgreSQL database instead, see section 12.

1. Create a free account at [Alwaysdata](https://www.alwaysdata.com/en/register/).
  Registration requires card validation; Alwaysdata says no fees will be charged.
2. In its administration panel, add a site at `https://<account>.alwaysdata.net`, choose
  **Python WSGI**, and enable **Force HTTPS** in the site's SSL settings.
3. Download the project ZIP from the private
  [GitHub repository](https://github.com/mounetnotamuriellemerveil-sketch/road-speed-regulation)
  and upload/extract it using SFTP into `/home/<account>/road-speed-regulation`.
  The ZIP does not include the ignored local database, credentials, or `config.env`.
4. In an Alwaysdata console, install the dependencies:
  ```
  cd ~/road-speed-regulation
  python -m venv .venv
  source .venv/bin/activate
  python -m pip install -r requirements.txt
  ```
5. Set the site's WSGI application path to
  `/home/<account>/road-speed-regulation/wsgi.py` and select
  `/home/<account>/road-speed-regulation/.venv` as its virtualenv. Reload the site.
6. Read `first_run_credentials.txt` in the project directory using SFTP. Hand the temporary
  credentials to the intended users, then delete the file and have everyone change their
  passwords after signing in.

Replace `<account>` with your Alwaysdata account name. The site URL is
`https://<account>.alwaysdata.net`. Keep the project directory, especially `road_speed.db`,
when uploading future code updates. Outbound SMTP and MQTT access depend on the provider's
network rules; test those integrations before relying on them. Without email settings,
messages are written to the site's logs instead.

## 2. Local development setup (optional)
1. Install Python from python.org. During installation tick **"Add Python to PATH"**.
2. Put all the files in one folder, e.g. `C:\road-speed`.
3. Open that folder in File Explorer, click the address bar, type `cmd`, press Enter.
4. Run:
   ```
   pip install -r requirements.txt
   ```

## 3. Set up email (for local development)
1. Copy `config.env.example` and rename the copy to `config.env`.
2. Fill in your Gmail address and a Gmail **App password**
   (Google account → Security → 2-Step Verification on → App passwords).
   If email ports are blocked, set `BREVO_API_KEY` and `BREVO_SENDER` instead.
3. Check it with `python send_driver_code.py --test`, then restart the server.
4. When deployed, set `APP_URL` to the public address (e.g. `https://<account>.alwaysdata.net`)
   so the sign-in link in emails points to it.

Without email settings nothing is sent: emails are printed in the server window, and the admin page
shows the code and temporary password of each new driver so the admin can hand them over.

## 4. Run locally for development
| Command | Use it when |
|---|---|
| `python app.py` | Using the app on this computer only. Open http://localhost:5000 |
| `python app.py --lan` | Drivers use **phones** on the same Wi-Fi. Open the `https://...` address the server prints, on the phone, in Chrome. Tap **Advanced → Proceed** on the certificate warning once. HTTPS is required for phone GPS. |

Keep the server window open. Stop it with **Ctrl + C**.

## 5. First sign-in
On first start the server writes **`first_run_credentials.txt`**: one code and temporary
password for each police station and each admin. Give each one to the right person,
then delete the file. Everyone is asked to choose their own password at first sign-in.

- **Super admin**: `ADM-SUPER`. Can register drivers, reset any password, and replace the
  "To confirm" station names (Edit name) once the official names are known.
- **Agency admins**: `ADM-GEV`, `ADM-FNX`, `ADM-GEX`, `ADM-BNM`, `ADM-TEX`, `ADM-SCF`.
- **Police**: `PS-DLA-01`, `PS-YDE-01` … `PS-YDE-04`, `PS-EDE-01`, `PS-MBG-01`,
  `PS-NKS-01`, `PS-DSC-01`, `PS-BAF-01`.
- **Drivers**: create an account on the Driver page (code arrives by email) or are
  registered by an admin. The admin's registration emails the driver code, agency, car code,
  name and a temporary password, with a link that opens the driver sign-in with everything
  but the password filled in. Someone who signs in with a temporary password must choose
  their own before using the app. If the email cannot be sent, the account is still created
  and the admin page shows the details to hand over; **Reset password** emails new ones later.

## 6. Speed source on the driver screen
- **Phone GPS** (default): real speed and position from the phone in the vehicle.
- **Test input**: speed from a slider, for showing the regulation indoors. The position moves
  along the straight line between the two towns, so roadside stations detect the vehicle as it
  goes. Labelled as test input everywhere it appears (driver screen, police live list, history).

## 7. Road segments and roadside stations
Each **road segment** is the stretch of road covered by one roadside station (RSU), defined by
the station's position and a coverage radius. The server creates them on first start (and when
upgrading an older database):
- one **urban segment** per town (`DLA`, `YDE`, …), radius 10 km, keeping the limit its police
  had already set;
- on each link of the corridor Bafoussam – Dschang – Nkongsamba – Mbanga – Douala – Edéa –
  Yaoundé, **one segment per side** (`DLA-EDE` is the Douala side of the Douala – Edéa road),
  owned by the nearer town's police. These start at a **placeholder of 80 km/h**: the super admin
  should enter the official limits.

Every second the vehicle sends its position. The station whose zone contains it **detects** the
vehicle (logged, and published on MQTT) and sends the segment's limit. Where zones overlap the
smallest wins. Between zones the vehicle keeps the last limit it received.

Who sets limits: the **super admin** for any segment (Admin page, "Road segments and speed
limits", also the coverage radius), and each **police station** for the segments of its own
town. Every change is kept in a change log.

## 8. Logging, alerts and history
- **Logging**: speed, position, segment and time are sampled every 5 s while driving
  (`telemetry` table, kept 180 days; change with `RSR_TELEMETRY_DAYS`). Each warning and speed
  reduction is stored with its location and the time it happened on the vehicle. If the
  connection drops, the driver page keeps the events and sends them again later; the server
  ignores duplicates.
- **Alerts** (police for their sector, admins for all or their agency; with a sound and a
  message on screen, and an Acknowledge button):

  | Level | Rule |
  |---|---|
  | Critical | Extreme speed: 50% or more, or 40 km/h or more, above the limit |
  | High | High-severity violation (more than 25% above the limit) |
  | High | Over the limit for 30 s or more (warn-only agency, or speed not coming down) |
  | Medium | Repeat offender: 3 violations by the same driver within 15 minutes |
- **History by road segment** (police and admin pages): choose a segment and a period to see
  speed samples, average, median, 85th percentile and maximum speed, time over the limit,
  violations by severity, regulations, alerts, a speed chart per hour or per day, each event
  with its location, and the limit changes. **Events CSV** and **Speeds CSV** download the data.

A web page cannot brake a vehicle. When regulation triggers, the app warns the driver
(screen, beep, vibration, voice) and publishes a **reduce** command for the vehicle unit
(see below). The physical speed reduction is done by that unit.

## 9. Connecting hardware (MQTT)
Set `MQTT_HOST` (and port/user/password) in `config.env`, then restart.

| Topic | Direction | Content |
|---|---|---|
| `corridor/limits/<segment>` | server → roadside unit | `{"segment","name","sector","rsu","limit_kmh","ts"}` (retained) |
| `corridor/rsu/<rsu>/detections` | server → roadside unit | a vehicle entered the station's zone |
| `corridor/vehicles/<CAR-CODE>/limit` | server → vehicle unit | limit sent when a station detects the vehicle |
| `corridor/vehicles/<CAR-CODE>/command` | server → vehicle unit | `{"action":"reduce"\|"warn"\|"release","target_kmh","rate_kmh_s",...}` |
| `corridor/signals/<sector>` | server → roadside unit | police messages |
| `corridor/alerts/<sector>` | server → control room | alerts (see section 8) |
| `corridor/speedevents` | vehicle unit → server | event JSON; the server stores it in the database |

Segment topics use the lower-case segment code (`dla`, `dla-ede`, `rsu-dla-ede`). Sector slugs
are lower case without accents: `douala`, `yaounde`, `edea`, `mbanga`, `nkongsamba`, `dschang`,
`bafoussam`. An event published on `corridor/speedevents` may include `lat`, `lon`, `segment` and
`device_ts` (milliseconds); without `segment`, the server finds it from the position.

A vehicle unit can also use HTTP directly: `POST /api/login` (role `driver`) to get a token,
then `POST /api/driver/tick` every second with `speed`, `lat`, `lon` and `status` (returns the
segment, its limit, whether a station detected the vehicle, the mode and the signals), and
`POST /api/driver/events` for each warning or reduction.

## 10. Testing the functional requirements
`tests/test_requirements.py` checks FR1 to FR12, one test per requirement, against a temporary
database (no email or MQTT is sent):
```
python -m unittest tests.test_requirements -v
```
FR4 to FR6 run on the driver page, so one test drives it in Chrome with Playwright
(`pip install playwright`); it is skipped when Playwright is not installed.

The tests always use a temporary SQLite database, even when `DATABASE_URL` is set. To run them on
PostgreSQL, set `RSR_TEST_DATABASE_URL` to an **empty** database, such as a new Neon branch made
for the purpose (never the one in use: the tests add their own accounts and data).

## 11. Optional paid deployment with Render
The app is a Flask service, not a static website. The included `render.yaml` configures
Render with HTTPS and a persistent disk for the database and initial credentials. A
persistent disk requires a paid Render service.

1. Revoke the Gmail app password currently in your local `config.env` and create a new one.
  Never commit `config.env`; enter the replacement in Render's secret settings instead.
2. Push this project to a GitHub repository, making sure `config.env`, `road_speed.db`,
  and `first_run_credentials.txt` are not included.
3. In Render, choose **New > Blueprint**, connect the repository, and apply `render.yaml`.
  Set `SMTP_USER`, `SMTP_PASS`, and `SMTP_FROM` when prompted for environment values, and
  `DATABASE_URL` to the Neon connection string (section 12), or leave it empty to use SQLite on the disk.
4. When the service is live, open its **Shell** and read
  `/var/data/first_run_credentials.txt`. Give each temporary credential to its intended
  user, then delete that file from the persistent disk. Users should change their passwords
  after signing in.

Render provides the public HTTPS address after deployment. Keep the service and its disk;
the disk contains the production database. Back it up regularly. `python app.py` remains
available for local development, but is not needed to use the deployed app.

## 12. Database on Neon (PostgreSQL)
Without `DATABASE_URL` the server keeps its data in the SQLite file `road_speed.db`. To keep it in a
[Neon](https://neon.tech) PostgreSQL database instead:

1. Create a project in Neon. Choose the **region closest to where the server runs** (for example
  AWS Europe – Frankfurt for Alwaysdata, which is in Paris): each vehicle update, once a second, makes
  several queries, and every one of them travels to the database and back.
2. In the Neon console, click **Connect** and copy the connection string
  (`postgresql://...neon.tech/neondb?sslmode=require...`).
3. Put it in `config.env` for local use, or in the host's environment variables when deployed:
  ```
  DATABASE_URL=postgresql://USER:PASSWORD@ep-....neon.tech/neondb?sslmode=require&channel_binding=require
  ```
4. Install the dependencies again (`pip install -r requirements.txt`, which adds `psycopg`).
5. To keep the existing accounts, passwords and history, copy `road_speed.db` into Neon **before the
  server first starts on it**:
  ```
  python import_sqlite.py
  ```
  It copies only into an empty Neon database and only reads `road_speed.db`. Skip this step to start
  with no data: the server then creates the tables and writes a new `first_run_credentials.txt` (section 5).
6. Start the server. It prints `Database: Neon ep-....neon.tech/neondb`, and `/api/health` shows
  `"database": "postgres"`.

The server creates and upgrades its tables itself; there is nothing to run in Neon. Notes:
- Until each person chooses their own password, the temporary ones are also in the `issued_credentials`
  table (Neon **SQL Editor**: `SELECT * FROM issued_credentials;`). This helps when the host does not keep
  `first_run_credentials.txt`.
- On Neon's free plan the database pauses after a few minutes without use. The first request after
  that takes a moment longer while it starts again.
- With Neon, the data no longer depends on the host's disk, so it is kept across redeploys.

## Troubleshooting
- **"python is not recognized"** → reinstall Python with "Add to PATH", or use `py app.py`.
- **Page says "The server is not running"** → you opened the file directly. Start the server
  and open the address it prints.
- **Phone cannot open the address** → phone and computer must be on the same Wi-Fi; allow
  Python through the Windows firewall when asked.
- **GPS stays at "Waiting"** → allow location for the site in Chrome, and go outdoors or near
  a window.
- **Start from zero** → stop the server, delete `road_speed.db` and
  `first_run_credentials.txt`, start again. With Neon, point `DATABASE_URL` to a new, empty database or
  branch instead of deleting `road_speed.db`.
- **Server stops at start-up with "connection failed"** → it cannot reach Neon. The message gives the
  reason: check the `DATABASE_URL` copied from the Neon console (user, password, host).
