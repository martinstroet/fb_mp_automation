"""Gmail SMTP notifications: immediate hot alerts + daily digest.

Thumbnails are attached as inline CID parts from local files (FB CDN URLs
expire, so emails must never hot-link them)."""

from __future__ import annotations

import datetime as dt
import logging
import smtplib
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


def send(email_cfg, msg: EmailMessage):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(email_cfg.address, email_cfg.app_password)
        s.send_message(msg)


def compose_hot(email_cfg, listing: dict, ev: dict, target_id: str,
                to: str | None = None) -> EmailMessage:
    est = ev.get("est_value_aud")
    subject = f"🔥 FB MP: {listing.get('title') or 'listing'} — {listing.get('price_text') or '?'}"
    if est:
        subject += f" (est ${est})"
    cid = new_cid(listing.get("thumb_path"))
    html = _env.get_template("hot.html.j2").render(
        l=listing, ev=ev, target_id=target_id, thumb_cid=cid
    )
    images = {cid: listing["thumb_path"]} if cid else {}
    return build_html_email(email_cfg, subject, html, images, to=to)


def send_hot(email_cfg, listing: dict, ev: dict, target_id: str, to: str | None = None):
    send(email_cfg, compose_hot(email_cfg, listing, ev, target_id, to=to))


def compose_digest(email_cfg, groups: dict[str, list[dict]], offers: list[dict],
                   flagged: list[dict], health: dict, to: str | None = None) -> EmailMessage:
    images: dict[str, str] = {}
    for items in list(groups.values()) + [offers, flagged]:
        for it in items:
            cid = new_cid(it.get("thumb_path"))
            it["thumb_cid"] = cid
            if cid:
                images[cid] = it["thumb_path"]
    n = sum(len(v) for v in groups.values()) + len(offers) + len(flagged)
    subject = f"FB Marketplace digest — {n} listing{'s' if n != 1 else ''}"
    html = _env.get_template("digest.html.j2").render(
        date=dt.date.today().strftime("%a %d %b %Y"),
        groups=groups,
        offers=offers,
        flagged=flagged,
        health=health,
    )
    return build_html_email(email_cfg, subject, html, images, to=to)


def send_digest(email_cfg, groups: dict, offers: list, flagged: list, health: dict,
                to: str | None = None):
    send(email_cfg, compose_digest(email_cfg, groups, offers, flagged, health, to=to))


def send_plain(email_cfg, subject: str, body: str):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = email_cfg.address, email_cfg.to, subject
    msg.set_content(body)
    send(email_cfg, msg)
