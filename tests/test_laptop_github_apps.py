"""`valxel dev github-apps`: the laptop's own GitHub Apps, registered through GitHub's manifest flow,
so the sign-up wizard's GitHub step runs for real (valxel_sdlc.laptop.github_apps)."""

import json
import re
import threading
import urllib.request
from html import unescape
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from valxel_sdlc import laptop
from valxel_sdlc.integrations.github import (
    DELIVERY_PERMISSIONS,
    PROVISIONING_PERMISSIONS,
)
from valxel_sdlc.laptop import github_apps
from valxel_sdlc.signing import github_app_signer


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _created(app: str, pem: str) -> dict:
    return {"id": 100 if app == "delivery" else 200, "slug": f"valxel-laptop-{app}", "client_id": f"Iv1.{app}",
            "client_secret": f"cs-{app}", "webhook_secret": f"wh-{app}", "pem": pem}


def test_manifest_asks_for_the_install_time_oauth_code_and_the_tool_permissions():
    m = github_apps.manifest("delivery", name="Valxel laptop delivery", port=8090, redirect="http://127.0.0.1:5/created")
    assert m["request_oauth_on_install"] is True and m["setup_on_update"] is True and m["public"] is False
    assert m["callback_urls"] == ["http://localhost:8090/api/connections/github/callback",
                                  "http://localhost:8080/api/connections/github/callback"]
    assert m["setup_url"] == m["callback_urls"][0]
    assert m["hook_attributes"]["active"] is False  # GitHub can't reach a laptop
    assert m["default_permissions"] == DELIVERY_PERMISSIONS
    assert github_apps.manifest("provisioner", name="p", port=8080, redirect="r")["default_permissions"] == \
        PROVISIONING_PERMISSIONS
    assert github_apps.new_app_url("acme-farm", "s1") == "https://github.com/organizations/acme-farm/settings/apps/new?state=s1"
    assert github_apps.new_app_url("", "s1") == "https://github.com/settings/apps/new?state=s1"


def test_a_one_line_key_from_env_signs():
    pem = _pem()
    env = github_apps.settings_from("delivery", _created("delivery", pem))
    assert "\n" not in env["VALXEL_GITHUB_APP_PRIVATE_KEY"]
    signer = github_app_signer({"VALXEL_GITHUB_APP_ID": "100", **env})
    assert len(signer.sign(b"jwt")) == 256
    prov = github_apps.settings_from("provisioner", _created("provisioner", pem))
    assert prov["VALXEL_GITHUB_PROVISIONER_APP_ID"] == "200" and "VALXEL_GITHUB_WEBHOOK_SECRET" not in prov
    assert github_app_signer({**prov}, app="provisioner")


def _drive(state_seen: list, *, bad_state=False):
    """Plays the browser and GitHub: load the local page, read the form, come back with a code."""

    def open_browser(url):
        def go():
            for _ in range(2):
                page = urllib.request.urlopen(url, timeout=10).read().decode()
                action = unescape(re.search(r'action="([^"]+)"', page).group(1))
                manifest = json.loads(unescape(re.search(r'name="manifest" value="([^"]+)"', page).group(1)))
                state = parse_qs(urlparse(action).query)["state"][0]
                state_seen.append((manifest["name"], state))
                back = urlparse(manifest["redirect_url"])
                code_state = "forged" if bad_state else state
                try:
                    urllib.request.urlopen(f"http://{back.netloc}/created?code=c-{len(state_seen)}&state={code_state}",
                                           timeout=10).read()
                except urllib.error.HTTPError:
                    return
        threading.Thread(target=go, daemon=True).start()

    return open_browser


def test_register_runs_both_apps_and_saves_their_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(laptop, "docker_gid", lambda: "0")
    laptop.init(target=tmp_path, anthropic_key="k")
    pem, seen, codes = _pem(), [], []

    def convert(code):
        codes.append(code)
        return _created("delivery" if len(codes) == 1 else "provisioner", pem)

    made = github_apps.register(tmp_path, org="acme-farm", prefix="Valxel test", open_browser=_drive(seen),
                                convert_fn=convert, timeout=20)
    assert list(made) == ["delivery", "provisioner"] and codes == ["c-1", "c-2"]
    assert [n for n, _ in seen] == ["Valxel test delivery", "Valxel test provisioner"]
    env = laptop.read_env(tmp_path / ".env")
    assert env["VALXEL_GITHUB_APP_SLUG"] == "valxel-laptop-delivery" and env["VALXEL_GITHUB_APP_CLIENT_SECRET"] == "cs-delivery"
    assert env["VALXEL_GITHUB_PROVISIONER_APP_ID"] == "200" and env["VALXEL_GITHUB_WEBHOOK_SECRET"] == "wh-delivery"
    assert env["ANTHROPIC_API_KEY"] == "k"  # the rest of .env stays
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"


def test_a_return_without_this_flows_state_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(laptop, "docker_gid", lambda: "0")
    laptop.init(target=tmp_path, anthropic_key="k")
    before = (tmp_path / ".env").read_text()
    with pytest.raises(RuntimeError, match="state"):
        github_apps.register(tmp_path, open_browser=_drive([], bad_state=True),
                             convert_fn=lambda code: pytest.fail("must not exchange"), timeout=20)
    assert (tmp_path / ".env").read_text() == before


def test_compose_gives_the_tool_the_apps_settings():
    spec = yaml.safe_load((laptop.bundle() / "compose.yaml").read_text())
    for svc in ("api", "worker"):
        env = spec["services"][svc]["environment"]
        for k in ("VALXEL_GITHUB_APP_SLUG", "VALXEL_GITHUB_APP_CLIENT_ID", "VALXEL_GITHUB_APP_CLIENT_SECRET",
                  "VALXEL_GITHUB_PROVISIONER_APP_ID", "VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY",
                  "VALXEL_GITHUB_WEBHOOK_SECRET"):
            assert k in env


def test_a_laptop_with_its_own_apps_creates_repos_with_the_provisioning_app(monkeypatch, tmp_path):
    from pathlib import Path

    from valxel_sdlc.config import load_settings
    from valxel_sdlc.services import default_provisioner

    pem = _pem()
    for k, v in {**github_apps.settings_from("provisioner", _created("provisioner", pem)),
                 "VALXEL_DELIVERY": "local"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=Path(__file__).resolve().parents[1] / "examples" / "governance")
    sentinel = object()
    prov = default_provisioner(s, sentinel, None)
    assert prov.github is not sentinel  # its own token provider, with repo-creation permissions
    assert prov.github.tokens.permissions == PROVISIONING_PERMISSIONS


def test_installed_on_signs_as_each_app_and_lists_its_installs():
    import io

    pem = _pem()
    env = {**github_apps.settings_from("delivery", _created("delivery", pem)),
           **github_apps.settings_from("provisioner", _created("provisioner", pem))}
    seen = []

    class Opener:
        def open(self, req, timeout=None):
            seen.append((req.full_url, req.headers.get("Authorization", "")))
            return io.BytesIO(json.dumps([{"account": {"login": "acme-farm"}}]).encode())

    got = github_apps.installed_on(env, opener=Opener())
    assert got == {"delivery": ["acme-farm"], "provisioner": ["acme-farm"]}
    assert all(u.endswith("/app/installations") and a.startswith("Bearer ey") for u, a in seen)
    assert github_apps.installed_on({}) == {}
