"""Gmail SMTP notifications: immediate hot alerts + daily digest.

Thumbnails are attached as inline CID parts from local files (FB CDN URLs
expire, so emails must never hot-link them)."""

from __future__ import annotations

import datetime as dt
import logging
import smtplib
import socket
import time
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True)


def new_cid(path: str | None) -> str | None:
    """A fresh Content-ID for a local image path, or None if the file is absent."""
    if path and Path(path).exists():
        return make_msgid(domain="fbmp.local")[1:-1]  # strip <>
    return None


def build_html_email(email_cfg, subject: str, html: str, inline_images: dict[str, str],
                     to: str | None = None) -> EmailMessage:
    """inline_images: {cid: local_path} — cids must already appear in the html.
    `to` overrides the default destination (per-target routing)."""
    msg = EmailMessage()
    msg["From"] = email_cfg.address
    msg["To"] = to or email_cfg.to
    msg["Subject"] = subject
    msg.add_alternative(html, subtype="html")
    html_part = msg.get_payload()[0]
    for cid, path in inline_images.items():
        data = Path(path).read_bytes()
        subtype = "png" if path.lower().endswith(".png") else "jpeg"
        html_part.add_related(data, "image", subtype, cid=f"<{cid}>")
    return msg


# Transient: the machine's DNS or network was briefly unavailable, or Gmail
# dropped the connection. Retrying inside the run beats the caller's
# retry-tomorrow fallback, which costs a whole day of the digest.
_RETRYABLE = (socket.gaierror, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
              TimeoutError, ConnectionError, OSError)
_SEND_BACKOFF = (5, 20)  # seconds before attempts 2 and 3
# Socket timeout. Since Python 3.5 this caps the whole sendall() of the
# message, not each chunk — a ~1.3MB digest over a slow uplink overran 30s
# and failed as "Server not connected" (Sep 2026).
_SEND_TIMEOUT = 120


def send(email_cfg, msg: EmailMessage):
    """Send one message, retrying transient network failures. Auth and recipient
    errors are permanent — they raise on the first attempt so the caller can roll
    its alert claim back immediately."""
    for attempt, pause in enumerate((*_SEND_BACKOFF, None), start=1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=_SEND_TIMEOUT) as s:
                s.login(email_cfg.address, email_cfg.app_password)
                s.send_message(msg)
            return
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused):
            raise
        except _RETRYABLE as e:
            if pause is None:
                raise
            log.warning("smtp send attempt %d failed (%s) — retrying in %ds",
                        attempt, e, pause)
            time.sleep(pause)


def compose_hot(email_cfg, listing: dict, ev: dict, target_id: str,
                to: str | None = None) -> EmailMessage:
    est = ev.get("est_value_aud")
    # The fire icon marks a genuine steal only; at/below-market finds are still
    # emailed on the spot, just without the flag.
    flag = "🔥 " if ev.get("bargain_rating") == "well_below_market" else ""
    subject = f"{flag}FB MP: {listing.get('title') or 'listing'} — {listing.get('price_text') or '?'}"
    if est:
        subject += f" (est ${est})"
    cid = new_cid(listing.get("thumb_path"))
    html = _env.get_template("hot.html.j2").render(
        l=listing, ev=ev, target_id=target_id, thumb_cid=cid, flag=flag
    )
    images = {cid: listing["thumb_path"]} if cid else {}
    return build_html_email(email_cfg, subject, html, images, to=to)


def send_hot(email_cfg, listing: dict, ev: dict, target_id: str, to: str | None = None):
    send(email_cfg, compose_hot(email_cfg, listing, ev, target_id, to=to))


def compose_digest(email_cfg, groups: dict[str, list[dict]], offers: list[dict],
                   flagged: list[dict], health: dict, to: str | None = None,
                   summary: list[tuple[str, str]] | None = None) -> EmailMessage:
    """`summary`: (label, value) rows for the owner's daily oversight block."""
    images: dict[str, str] = {}
    for items in list(groups.values()) + [offers, flagged]:
        for it in items:
            cid = new_cid(it.get("thumb_path"))
            it["thumb_cid"] = cid
            if cid:
                images[cid] = it["thumb_path"]
    n = sum(len(v) for v in groups.values()) + len(offers) + len(flagged)
    if n:
        subject = f"FB Marketplace digest — {n} listing{'s' if n != 1 else ''}"
    else:
        subject = "FB Marketplace daily summary — no new listings"
    html = _env.get_template("digest.html.j2").render(
        date=dt.date.today().strftime("%a %d %b %Y"),
        groups=groups,
        offers=offers,
        flagged=flagged,
        health=health,
        summary=summary,
    )
    return build_html_email(email_cfg, subject, html, images, to=to)


def send_digest(email_cfg, groups: dict, offers: list, flagged: list, health: dict,
                to: str | None = None, summary: list[tuple[str, str]] | None = None):
    send(email_cfg, compose_digest(email_cfg, groups, offers, flagged, health,
                                   to=to, summary=summary))


def send_plain(email_cfg, subject: str, body: str):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = email_cfg.address, email_cfg.to, subject
    msg.set_content(body)
    send(email_cfg, msg)
