"""Send the one invite email through Gmail SMTP (stdlib smtplib, STARTTLS).

Configured from the environment only, never from a file in the repo:

    PEOPLE_SMTP_USER       the sending Gmail address (also the From address)
    PEOPLE_SMTP_PASSWORD   its Google app password; whitespace is dropped.
                           Filled from the master file's slot
                           PARKED_LOGTO_SMTP_APP_PASSWORD (docs/runbook.md
                           "Email sign-in (Gmail SMTP)").
    PEOPLE_SMTP_FROM_NAME  optional display name
    PEOPLE_SMTP_HOST, PEOPLE_SMTP_PORT   rehearsal overrides (default
                           smtp.gmail.com:587)
    PEOPLE_SMTP_CA_FILE    optional CA bundle to trust instead of the system's
                           (for rehearsals against a local server)

Without the password the transport is "not configured": callers print the
draft instead of sending (see configured()).
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

HOST = "smtp.gmail.com"
PORT = 587
TIMEOUT = 30


class MailError(Exception):
    pass


def settings(env=None) -> dict | None:
    """The transport settings, or None when the app password is not set."""
    env = os.environ if env is None else env
    password = "".join(env.get("PEOPLE_SMTP_PASSWORD", "").split())
    user = env.get("PEOPLE_SMTP_USER", "").strip()
    if not password or not user:
        return None
    return {
        "user": user,
        "password": password,
        "name": env.get("PEOPLE_SMTP_FROM_NAME", "").strip(),
        "host": env.get("PEOPLE_SMTP_HOST", "").strip() or HOST,
        "port": int(env.get("PEOPLE_SMTP_PORT", "") or PORT),
        "cafile": env.get("PEOPLE_SMTP_CA_FILE", "").strip() or None,
    }


def split_message(text: str) -> tuple[str, str]:
    """Split a rendered template into (subject, body): a leading Subject: line."""
    head, _, rest = text.partition("\n")
    if not head.startswith("Subject:"):
        raise MailError("the rendered email has no Subject: line")
    return head[len("Subject:") :].strip(), rest.lstrip("\n")


def build(cfg: dict, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["name"], cfg["user"])) if cfg["name"] else cfg["user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send(cfg: dict, to: str, subject: str, body: str, context=None) -> None:
    """Send one message. Raises MailError; the password is never in the text."""
    msg = build(cfg, to, subject, body)
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls(
                context=context or ssl.create_default_context(cafile=cfg.get("cafile"))
            )
            smtp.ehlo()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        text = str(err).replace(cfg["password"], "***")
        raise MailError(f"sending to {to} failed: {text}") from None
