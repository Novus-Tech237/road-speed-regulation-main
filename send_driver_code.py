"""
send_driver_code.py - delivers driver codes (and temporary passwords) by email.

Configuration (config.env or environment variables):
    BREVO_API_KEY   if set, emails are sent through the Brevo web API (works when email ports are blocked)
    BREVO_SENDER    sender address verified in your Brevo account

    SMTP_HOST       e.g. smtp.gmail.com          (if unset, the email is printed to the console)
    SMTP_PORT       e.g. 587 (STARTTLS) or 465 (SSL)
    SMTP_USER       SMTP login
    SMTP_PASS       SMTP password / app password
    SMTP_FROM       sender address, defaults to SMTP_USER
    SMTP_SSL        optional; port 465 automatically uses SSL, 587 uses STARTTLS

Check the settings:   python send_driver_code.py --test

Command-line use (for testing):
    python send_driver_code.py driver@example.com "Paul Mbarga" DRV-GEV-4821
"""
import os
import smtplib
import sys
from email.message import EmailMessage

APP_NAME = "Road speed regulation"


def _load_config():
    """Read SMTP settings from config.env next to this file (environment variables win)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_config()


def _compose(to_email, full_name, driver_code, agency=None, temp_password=None, car_code=None, sign_in_url=None):
    lines = [
        f"Hello {full_name},",
        "",
        f"Your {APP_NAME} driver account is ready.",
        "",
    ]
    if sign_in_url:
        lines += ["Open this link to sign in. It fills in your details, except the password:", sign_in_url, ""]
    lines += ["Your sign-in details:", f"  Driver code:  {driver_code}"]
    if agency:
        lines.append(f"  Agency:       {agency}")
    if car_code:
        lines.append(f"  Car code:     {car_code}")
    lines.append(f"  Name:         {full_name}")
    if temp_password:
        lines += [
            f"  Temporary password: {temp_password}",
            "",
            "This password was issued by your agency administrator. Keep it private.",
            "You will be asked to choose your own password when you first sign in.",
        ]
    else:
        lines += ["", "Sign in with this code and the password you chose when you created the account."]
    lines += [
        "",
        "On the sign-in page choose Driver, enter the details above and your password, then choose your trip.",
        "",
        "If you did not request this account, ignore this email.",
        "",
        f"{APP_NAME} - roadside-to-vehicle speed regulation",
    ]
    msg = EmailMessage()
    msg["Subject"] = f"Your driver code: {driver_code}"
    msg["To"] = to_email
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "no-reply@localhost"
    msg.set_content("\n".join(lines))
    return msg


def _send_brevo(msg, to_email, full_name):
    """Send through the Brevo web API (HTTPS port 443 - works where email ports are blocked)."""
    import json
    import urllib.error
    import urllib.request
    sender = os.environ.get("BREVO_SENDER") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")
    if not sender:
        raise RuntimeError("set BREVO_SENDER in config.env (the sender address you verified in Brevo)")
    body = json.dumps({
        "sender": {"name": os.environ.get("BREVO_SENDER_NAME", APP_NAME), "email": sender},
        "to": [{"email": to_email, "name": full_name}],
        "subject": msg["Subject"],
        "textContent": msg.get_content(),
    }).encode("utf-8")
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=body, method="POST",
                                 headers={"api-key": os.environ["BREVO_API_KEY"].strip(),
                                          "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Brevo refused the email ({exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Brevo ({exc.reason}) - check the internet connection") from None


def send_account_email(to_email, full_name, role_label, code, temp_password, sign_in_as, sign_in_url=None):
    """Email a police station or admin account created by the super admin."""
    msg = EmailMessage()
    msg["Subject"] = f"Your {APP_NAME} account: {code}"
    msg["To"] = to_email
    msg["From"] = os.environ.get("BREVO_SENDER") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "no-reply@localhost"
    msg.set_content("\n".join([
        f"Hello {full_name},", "",
        f"A {role_label} account was created for you in {APP_NAME}.", "",
        *([f"Sign in here: {sign_in_url}", ""] if sign_in_url else []),
        f"Code: {code}",
        f"Temporary password: {temp_password}", "",
        f"To sign in, open the app, choose {sign_in_as}, and enter this code and password.",
        "You will be asked to choose your own password at the first sign-in.", "",
        "Keep these details private.", "",
        f"{APP_NAME} - roadside-to-vehicle speed regulation",
    ]))
    return _deliver(msg, to_email, full_name)


def send_driver_code(to_email, full_name, driver_code, agency=None, temp_password=None, car_code=None, sign_in_url=None):
    """Send the email. Returns 'brevo' or 'smtp' when sent, 'console' when printed instead.
    Raises on failure so the caller can report it."""
    return _deliver(_compose(to_email, full_name, driver_code, agency, temp_password, car_code, sign_in_url),
                    to_email, full_name)


def _deliver(msg, to_email, full_name):
    if os.environ.get("BREVO_API_KEY"):
        _send_brevo(msg, to_email, full_name)
        return "brevo"
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("\n" + "=" * 64)
        print(" SMTP not configured - email printed to console instead")
        print("=" * 64)
        print(f"To: {msg['To']}\nSubject: {msg['Subject']}\n")
        print(msg.get_content())
        print("=" * 64 + "\n", flush=True)
        return "console"

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = (os.environ.get("SMTP_PASS") or "").replace(" ", "")  # Gmail app passwords are shown with spaces
    first = "ssl" if (port == 465 or os.environ.get("SMTP_SSL") == "1") else "starttls"
    attempts = [(first, port)]
    # If the first way is blocked or cut off, try the other standard Gmail port.
    attempts.append(("starttls", 587) if first == "ssl" else ("ssl", 465))
    errors = []
    for mode, p in attempts:
        try:
            _send(msg, host, p, mode, user, password)
            return "smtp"
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError("Gmail refused the sign-in. Check SMTP_USER and the 16-letter App password "
                               "(2-Step Verification must be on).") from None
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as exc:
            errors.append(f"port {p} ({mode}): {exc or type(exc).__name__}")
    raise RuntimeError("could not reach the mail server - " + "; ".join(errors) +
                       ". Your network or antivirus may be blocking email ports")


def _send(msg, host, port, mode, user, password):
    if mode == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
    with s:
        if mode != "ssl":
            s.ehlo()
            s.starttls()
            s.ehlo()
        if user:
            s.login(user, password)
        s.send_message(msg)


def diagnose():
    """python send_driver_code.py --test : checks each step and says what is wrong."""
    import socket
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = (os.environ.get("SMTP_PASS") or "").replace(" ", "")
    print("send_driver_code.py version: 3 (SMTP 587/465 + Brevo)")
    if os.environ.get("BREVO_API_KEY"):
        print("Brevo API key found - emails go through Brevo (port 443).")
        to = input("Send a test email to which address? ").strip()
        try:
            send_driver_code(to, "Test", "DRV-TEST-0000")
            print("Brevo: sent. Check the inbox and the Spam folder.")
        except Exception as exc:  # noqa: BLE001
            print("Brevo: FAILED ->", exc)
        return
    print("config.env found:", os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")))
    print("SMTP_HOST:", host or "(missing)")
    print("SMTP_USER:", user or "(missing)")
    print("SMTP_PASS:", f"{len(pw)} characters" + ("" if len(pw) == 16 else "  <- a Gmail App password has 16 letters"))
    if not host:
        return
    for port, mode in ((587, "starttls"), (465, "ssl")):
        print(f"\nPort {port} ({mode}):")
        try:
            socket.create_connection((host, port), timeout=10).close()
            print("  network: open")
        except OSError as exc:
            print(f"  network: BLOCKED ({exc})")
            continue
        try:
            s = smtplib.SMTP_SSL(host, port, timeout=20) if mode == "ssl" else smtplib.SMTP(host, port, timeout=20)
            with s:
                if mode != "ssl":
                    s.ehlo(); s.starttls(); s.ehlo()
                print("  secure connection: OK")
                s.login(user, pw)
                print("  Gmail sign-in: OK  -> email will work. Use SMTP_PORT=" + str(port))
                return
        except smtplib.SMTPAuthenticationError:
            print("  Gmail sign-in: REFUSED -> wrong App password or 2-Step Verification is off")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {exc or type(exc).__name__}  -> often an antivirus 'mail shield' or a filtered network")
    print("\nNo port worked. Try another connection (e.g. phone hotspot) or pause the antivirus mail scanning.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        diagnose()
        sys.exit(0)
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    print("Delivered via:", send_driver_code(sys.argv[1], sys.argv[2], sys.argv[3]))
