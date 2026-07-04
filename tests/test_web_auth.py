"""Login-form auth flow — offline, using FastAPI's TestClient.

Covers the password-manager-friendly session login, the Basic-auth fallback
for scripts, and the public paths (login page, icon).
"""

from __future__ import annotations

import base64
import importlib
import os


def _client(tmp_path, password: str = "testpw123"):
    os.environ["CREMA_DB_PATH"] = str(tmp_path / "t.db")
    os.environ["CREMA_WEB_PASSWORD"] = password
    os.environ["CREMA_WEB_USER"] = "crema"
    import crema.web.app as w

    importlib.reload(w)
    from fastapi.testclient import TestClient

    return w, TestClient(w.app, follow_redirects=False)


def test_browser_is_redirected_to_login_and_form_is_pw_manager_friendly(tmp_path):
    _, client = _client(tmp_path)
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 307 and r.headers["location"] == "/login"
    r = client.get("/login")
    assert 'autocomplete="username"' in r.text
    assert 'autocomplete="current-password"' in r.text


def test_login_sets_session_cookie_and_grants_access(tmp_path):
    _, client = _client(tmp_path)
    origin = {"Origin": "http://testserver"}
    r = client.post("/login", data={"username": "crema", "password": "wrong"}, headers=origin)
    assert r.status_code == 401
    r = client.post("/login", data={"username": "crema", "password": "testpw123"}, headers=origin)
    assert r.status_code == 303 and "crema_session" in r.headers.get("set-cookie", "")
    token = r.headers["set-cookie"].split("crema_session=")[1].split(";")[0]
    client.cookies.set("crema_session", token)
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200 and "Sign out" in r.text


def test_basic_auth_still_works_for_scripts_and_icon_is_public(tmp_path):
    _, client = _client(tmp_path)
    creds = base64.b64encode(b"crema:testpw123").decode()
    assert client.get("/", headers={"Authorization": f"Basic {creds}"}).status_code == 200
    assert client.get("/", headers={"Accept": "application/json"}).status_code == 401
    assert client.get("/icon.png").status_code == 200
