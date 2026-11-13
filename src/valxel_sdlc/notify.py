"""Tell people when they're needed, instead of making them check.

Each person chooses (on any feature page, or their settings): an email address, and whether to be
told when a decision waits for them, and when work they watch reaches staging. A decision waits for
someone when they match its approvers (a team they're in, or their login).

Channels, configured by the operator:

    VALXEL_SMTP_HOST (and _PORT, _USER, _PASSWORD, _FROM)   email to the person's address
    VALXEL_NOTIFY_WEBHOOK                                    a chat webhook (Slack-style {"text": ...})
                                                             for the team, naming who's needed

Messages carry the thread id, its title, what's waiting and a link: never document contents, so
nothing from a PHI product's documents leaves the tool. Each question is announced once.
Preferences are kept in <artifact dir>/notify.json (one replica); the outbox keeps the last 200
messages for the page that shows what was sent.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)
OUTBOX = 200


@dataclass
class Prefs:
    login: str
    email: str = ""
    needed: bool = True  # a decision waits for me
    staging: bool = False  # work I watch reached staging
    teams: list[str] = field(default_factory=list)  # kept from sign-in, to match approvers while signed out
    watch: list[str] = field(default_factory=list)  # thread ids


def _matches(p: Prefs, approver: str) -> bool:
    a = approver.strip().lstrip("@").lower()
    return a in {t.lower() for t in p.teams} if "/" in a else a == p.login.lower()


class Notifier:
    def __init__(self, root: Path, *, base_url: str = "", send_email=None, post=None) -> None:
        self.path = Path(root) / "notify.json"
        self.base_url = (base_url or os.environ.get("VALXEL_PUBLIC_URL", "")).rstrip("/")
        self._lock = threading.Lock()
        self._send_email = send_email or _smtp
        self._post = post or _webhook
        self.prefs: dict[str, Prefs] = {}
        self.announced: set[str] = set()
        self.outbox: list[dict] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.prefs = {k: Prefs(**v) for k, v in (data.get("prefs") or {}).items()}
                self.announced = set(data.get("announced") or [])
            except (ValueError, TypeError):
                log.warning("notify.json couldn't be read; starting with no preferences")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"prefs": {k: asdict(v) for k, v in self.prefs.items()},
                                             "announced": sorted(self.announced)[-5000:]}), encoding="utf-8")
        except OSError as exc:
            log.warning("couldn't save notification preferences: %s", exc)

    def get(self, login: str, teams: list[str] | None = None) -> Prefs:
        with self._lock:
            p = self.prefs.get(login) or Prefs(login=login)
            if teams is not None and teams != p.teams:
                p.teams = list(teams)
                self.prefs[login] = p
                self._save()
            return p

    def set(self, login: str, teams: list[str], **changes) -> Prefs:
        with self._lock:
            p = self.prefs.get(login) or Prefs(login=login)
            for k, v in changes.items():
                if v is not None and k in ("email", "needed", "staging"):
                    setattr(p, k, v)
            p.teams = list(teams)
            self.prefs[login] = p
            self._save()
            return p

    def watch(self, login: str, thread_id: str, on: bool) -> Prefs:
        with self._lock:
            p = self.prefs.get(login) or Prefs(login=login)
            p.watch = sorted(set(p.watch) | {thread_id}) if on else [w for w in p.watch if w != thread_id]
            self.prefs[login] = p
            self._save()
            return p

    def link(self, thread_id: str) -> str:
        kind = (thread_id.split("-") + ["", ""])[1]
        path = {"INI": "initiatives", "REL": "releases", "PRD": "products", "REV": "reviews",
                "INC": "incidents"}.get(kind, "features")
        return f"{self.base_url}/{path}/{thread_id}" if self.base_url else thread_id

    def _send(self, people: list[Prefs], subject: str, text: str, kind: str, ref: str) -> None:
        to = [p for p in people if p.email]
        msg = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "ref": ref, "subject": subject,
               "to": [p.login for p in people], "emailed": [p.login for p in to]}
        for p in to:
            try:
                self._send_email(p.email, subject, text)
            except Exception as exc:  # a failed email never stops the work
                msg.setdefault("errors", []).append(f"{p.login}: {type(exc).__name__}")
        if os.environ.get("VALXEL_NOTIFY_WEBHOOK") or self._post is not _webhook:
            try:
                self._post(f"{subject} ({', '.join('@' + p.login for p in people)})\n{text}")
                msg["posted"] = True
            except Exception as exc:
                msg.setdefault("errors", []).append(f"webhook: {type(exc).__name__}")
        self.outbox = (self.outbox + [msg])[-OUTBOX:]

    def needed(self, item: dict, thread_title: str = "") -> list[str]:
        """A decision is waiting: tell each person who can make it and asked to be told. Once."""
        ref = f"{item.get('feature_id')}:{item.get('interrupt_id')}"
        with self._lock:
            if ref in self.announced:
                return []
            self.announced.add(ref)
            people = [p for p in self.prefs.values() if p.needed
                      and any(_matches(p, a) for a in item.get("approvers") or [])]
            self._save()
        if not people:
            return []
        what = item.get("label") or item.get("gate", "").upper() or item.get("type", "a decision")
        tid = item.get("feature_id", "")
        subject = f"You're needed: {what} for {tid} {thread_title}".strip()
        self._send(people, subject, f"{tid} {thread_title} is waiting for {what}.\n{self.link(tid)}", "needed", ref)
        return [p.login for p in people]

    def reached(self, thread_id: str, title: str, where: str) -> list[str]:
        """Work reached a stage (staging): tell the people watching it who asked for that."""
        ref = f"{thread_id}:{where}"
        with self._lock:
            if ref in self.announced:
                return []
            self.announced.add(ref)
            people = [p for p in self.prefs.values() if p.staging and thread_id in p.watch]
            self._save()
        if people:
            self._send(people, f"{thread_id} {title} reached {where}", f"{thread_id} {title} reached {where}.\n"
                       f"{self.link(thread_id)}", "reached", ref)
        return [p.login for p in people]


def _smtp(to: str, subject: str, text: str) -> None:
    host = os.environ.get("VALXEL_SMTP_HOST", "")
    if not host:
        return
    m = EmailMessage()
    m["From"] = os.environ.get("VALXEL_SMTP_FROM", "valxel@localhost")
    m["To"], m["Subject"] = to, subject
    m.set_content(text)
    with smtplib.SMTP(host, int(os.environ.get("VALXEL_SMTP_PORT", "587")), timeout=10) as s:
        s.starttls()
        if os.environ.get("VALXEL_SMTP_USER"):
            s.login(os.environ["VALXEL_SMTP_USER"], os.environ.get("VALXEL_SMTP_PASSWORD", ""))
        s.send_message(m)


def _webhook(text: str) -> None:
    url = os.environ.get("VALXEL_NOTIFY_WEBHOOK", "")
    if not url:
        return
    req = Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
    urlopen(req, timeout=10).read()  # noqa: S310 - the operator's own webhook
