#!/usr/bin/env python3
"""Write /etc/jupiter-kiosk/secrets.json for the closet kiosk. Run as root on Jupiter.

  sudo python3 -I scripts/jupiter-kiosk/make_logins.py [--env /home/labadmin/gateway_nerves/.env]

(Not called make_secrets.py: the repo .gitignore drops every *secret* file.)

In this order, so a failure leaves nothing half-changed:
  1. Jess dashboard: derives the READ-ONLY kiosk session cookie, HMAC-SHA256(KIOSK_TOKEN,
     "brain-gateway-kiosk-session-v1") (frontend/src/lib/dashboard-auth.ts), so neither token is
     written here, and the full login (DASHBOARD_TOKEN's cookie) never is either. The kiosk session
     opens /dashboard and the GET calls that page makes, nothing else (lib/kiosk-session.ts). Then it
     checks the running dashboard: GET /dashboard must answer 200 (not a redirect to /login), and
     GET /chat and GET /api/proxy/api/inbox must answer 403 - proof that the frontend treats the
     cookie as read-only. The cookie and the admin password only go to a port the host's own
     docker-proxy holds (uid 0, host mount namespace, docker-proxy's inode): while a container is
     recreated, any local process could take the port.
  2. Grafana: creates the view-only user `closet-kiosk`, or re-enables it and gives it a fresh random
     password (set last), through the admin API on http://127.0.0.1:3000 with GF_SECURITY_ADMIN_USER /
     GF_SECURITY_ADMIN_PASSWORD from the repo .env. Org 1 role Viewer, never a Grafana admin. Never
     sent across a redirect.
  3. Writes the file root:kiosk 0640, atomically.

KIOSK_TOKEN (.env, at least 24 characters, not DASHBOARD_TOKEN) must be set and the frontend
recreated first. The cookie still shows what the dashboard shows (calendar, tasks, reminders,
self-care, finance snapshot), so keep the file private. Changing KIOSK_TOKEN logs out only the kiosk.
Re-run after KIOSK_TOKEN or the Grafana admin password changes, then
`sudo systemctl restart jupiter-kiosk`. Prints no secret values. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import grp
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ENV_DEFAULT = "/home/labadmin/gateway_nerves/.env"
SECRETS_PATH = "/etc/jupiter-kiosk/secrets.json"
GRAFANA = "http://127.0.0.1:3000"
DASHBOARD = "http://127.0.0.1:3001"
KIOSK_USER = "closet-kiosk"
KIOSK_GROUP = "kiosk"
DOCKER_PROXY = "/usr/bin/docker-proxy"
KIOSK_SESSION_LABEL = b"brain-gateway-kiosk-session-v1"  # frontend/src/lib/dashboard-auth.ts KIOSK_SESSION_LABEL
MIN_KIOSK_TOKEN_LENGTH = 24  # frontend/src/lib/dashboard-auth.ts MIN_KIOSK_TOKEN_LENGTH
DASHBOARD_COOKIE = "brain_token"  # frontend/src/lib/dashboard-auth.ts COOKIE_NAME
PLACEHOLDER_TOKENS = {"", "changeme"}  # the frontend refuses these too
# Must answer 403 to the kiosk cookie: a page and a proxy GET the dashboard page doesn't make.
KIOSK_REFUSED_PATHS = ("/chat", "/api/proxy/api/inbox")
REDIRECTS = (301, 302, 303, 307, 308)


class Refused(Exception):
    """Stop without changing anything further; the message is safe to print."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never re-send credentials to a Location
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines the way docker compose reads them for plain values: full-line comments skipped,
    a quoted value is what's inside its quotes (anything after the closing quote, like ' # note', is
    dropped), an unquoted value ends at ' #'. No interpolation."""
    values = {}
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        closing = value.find(value[0], 1) if value[:1] in ("'", '"') else -1
        if closing != -1:
            value = value[1:closing]
        else:
            cut = value.find(" #")
            if cut != -1:
                value = value[:cut].rstrip()
        values[key] = value
    return values


def kiosk_session(kiosk_token: str, dashboard_token: str) -> str:
    """The kiosk cookie value, after the frontend's rules for when kiosk sessions are on."""
    kiosk_token, dashboard_token = kiosk_token.strip(), dashboard_token.strip()  # the frontend trims both
    for name, value in (("DASHBOARD_TOKEN", dashboard_token), ("KIOSK_TOKEN", kiosk_token)):
        if "${" in value:
            raise Refused(f"{name} uses ${{...}} interpolation, which this script doesn't expand")
    if dashboard_token in PLACEHOLDER_TOKENS:
        raise Refused("DASHBOARD_TOKEN is unset or a placeholder: the dashboard has no login, so no kiosk login")
    if kiosk_token in PLACEHOLDER_TOKENS:
        raise Refused("KIOSK_TOKEN is not set: add one to the .env and recreate the frontend (README → Secrets)")
    if len(kiosk_token) < MIN_KIOSK_TOKEN_LENGTH:
        raise Refused(f"KIOSK_TOKEN is shorter than {MIN_KIOSK_TOKEN_LENGTH} characters: the frontend ignores it")
    if kiosk_token == dashboard_token:
        raise Refused("KIOSK_TOKEN is the same as DASHBOARD_TOKEN: the frontend ignores it; pick a new one")
    return hmac.new(kiosk_token.encode(), KIOSK_SESSION_LABEL, hashlib.sha256).hexdigest()


def _request(method: str, url: str, headers: dict[str, str], body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with _OPENER.open(request, timeout=15) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise Refused(f"{url.split('/api')[0]} not reachable ({type(e).__name__})") from None
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {}


def port_owner_problem(port: int) -> str | None:
    """None if 127.0.0.1:<port> is held by the host's own docker-proxy, else what's wrong.

    Checked before any credential is sent there: while a container is being recreated the port is
    free, and any local process - host-networked containers included - could take it. A process name
    proves nothing (anyone can set it), so this checks the listener's uid (root), its mount
    namespace (the host's, not a container's) and its executable (the same inode as docker-proxy).
    """
    try:
        out = subprocess.run(
            ["ss", "-Hltnp", f"src 127.0.0.1:{port}"], check=True, capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "ss failed"
    pids = sorted(set(re.findall(r"pid=(\d+)", out)))
    if not pids:
        return "nothing listens there"
    try:
        proxy = os.stat(DOCKER_PROXY)
        host_mnt = os.readlink("/proc/1/ns/mnt")
    except OSError:
        return f"can't check ({DOCKER_PROXY} or /proc/1 unreadable)"
    for pid in pids:
        try:
            owner_uid = os.stat(f"/proc/{pid}").st_uid
            exe = os.stat(f"/proc/{pid}/exe")
            mnt = os.readlink(f"/proc/{pid}/ns/mnt")
        except OSError:
            return f"pid {pid} vanished or is unreadable"
        if owner_uid != 0 or (exe.st_dev, exe.st_ino) != (proxy.st_dev, proxy.st_ino) or mnt != host_mnt:
            return f"held by pid {pid}, which is not the host's docker-proxy"
    return None


def require_docker_proxy(port: int, what: str) -> None:
    problem = port_owner_problem(port)
    if problem:
        raise Refused(f"127.0.0.1:{port}: {problem}; not sending the {what}")


def check_dashboard_cookie(session_value: str) -> None:
    """The running frontend opens /dashboard for the cookie, and refuses it everything else we try."""
    require_docker_proxy(3001, "dashboard cookie")
    cookie = {"Cookie": f"{DASHBOARD_COOKIE}={session_value}"}
    status, _ = _request("GET", f"{DASHBOARD}/dashboard", cookie)
    if status in REDIRECTS:
        raise Refused(
            "the dashboard rejects the kiosk cookie (redirects to /login): KIOSK_TOKEN in this .env differs "
            "from the running frontend's, or the frontend wasn't recreated after setting it"
        )
    if status != 200:
        raise Refused(f"the dashboard answered HTTP {status} for /dashboard: is the frontend up?")
    for path in KIOSK_REFUSED_PATHS:
        status, _ = _request("GET", f"{DASHBOARD}{path}", cookie)
        if status != 403:
            raise Refused(
                f"the dashboard answered HTTP {status} for {path} instead of 403: the running frontend doesn't "
                "treat the kiosk cookie as read-only, so it isn't written"
            )


def ensure_grafana_viewer(admin_user: str, admin_password: str) -> str:
    """Create, or re-enable and re-password, the kiosk's Grafana account; returns its new password.

    For an existing account the new password is set LAST (after enable, role and admin flag), and the
    caller writes the file straight after: a failure before that leaves the old password working.
    """
    require_docker_proxy(3000, "Grafana admin password")
    auth = {"Authorization": "Basic " + base64.b64encode(f"{admin_user}:{admin_password}".encode()).decode()}
    password = secrets.token_urlsafe(24) + "-Aa1"  # passes Grafana's password policy if it's ever turned on

    def ok(method: str, path: str, body: dict | None, failure: str) -> dict:
        status, answer = _request(method, f"{GRAFANA}{path}", auth, body)
        if status != 200:
            raise Refused(f"{failure} (HTTP {status})")
        return answer

    status, found = _request("GET", f"{GRAFANA}/api/users/lookup?loginOrEmail={KIOSK_USER}", auth)
    if status == 200 and found.get("id"):
        user_id, action = found["id"], "password reset"
    elif status == 404:
        created = ok(
            "POST",
            "/api/admin/users",
            {"name": "Closet kiosk", "login": KIOSK_USER, "password": password, "OrgId": 1},
            f"could not create {KIOSK_USER}",
        )
        user_id, action = created.get("id"), "created"
        if not user_id:
            raise Refused(f"Grafana created {KIOSK_USER} but returned no id")
    else:
        raise Refused(f"Grafana admin API answered HTTP {status} (is the admin password right?)")
    if action == "password reset":
        ok("POST", f"/api/admin/users/{user_id}/enable", None, f"could not enable {KIOSK_USER}")
    ok("PATCH", f"/api/orgs/1/users/{user_id}", {"role": "Viewer"}, f"could not make {KIOSK_USER} a Viewer in org 1")
    ok(
        "PUT",
        f"/api/admin/users/{user_id}/permissions",
        {"isGrafanaAdmin": False},
        f"could not clear Grafana admin on {KIOSK_USER}",
    )
    if action == "password reset":
        ok("PUT", f"/api/admin/users/{user_id}/password", {"password": password}, "could not set the new password")
    print(f"Grafana user {KIOSK_USER}: {action}, enabled, org 1 Viewer, not a Grafana admin")
    return password


def write_secrets(path: str, gid: int, data: dict[str, str]) -> None:
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".secrets.", suffix=".tmp")  # 0600, O_EXCL
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chown(tmp, 0, gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    print(f"wrote {path} (root:{KIOSK_GROUP} 0640)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the closet kiosk's logins (run as root on Jupiter)")
    parser.add_argument(
        "--env", default=ENV_DEFAULT, help="repo .env with KIOSK_TOKEN, DASHBOARD_TOKEN and GF_SECURITY_ADMIN_*"
    )
    parser.add_argument("--out", default=SECRETS_PATH)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("run as root (sudo): the file is root:kiosk 0640", file=sys.stderr)
        return 1
    try:
        try:
            gid = grp.getgrnam(KIOSK_GROUP).gr_gid
        except KeyError:
            raise Refused(f"no group {KIOSK_GROUP}: create the kiosk user first (README → Install)") from None
        if not os.path.isdir(os.path.dirname(args.out)):
            raise Refused(f"{os.path.dirname(args.out)} doesn't exist (README → Install)")
        try:
            with open(args.env, encoding="utf-8") as f:
                env = parse_env(f.read())
        except OSError as e:
            raise Refused(f"can't read {args.env} ({type(e).__name__})") from None
        admin_password = env.get("GF_SECURITY_ADMIN_PASSWORD", "")
        if not admin_password:
            raise Refused("GF_SECURITY_ADMIN_PASSWORD is not in the .env")
        session = kiosk_session(env.get("KIOSK_TOKEN", ""), env.get("DASHBOARD_TOKEN", ""))
        check_dashboard_cookie(session)
        print("dashboard kiosk cookie: opens /dashboard, refused elsewhere (read-only)")
        grafana_password = ensure_grafana_viewer(env.get("GF_SECURITY_ADMIN_USER") or "admin", admin_password)
        write_secrets(
            args.out,
            gid,
            {"grafana_user": KIOSK_USER, "grafana_password": grafana_password, "dashboard_kiosk_session": session},
        )
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
