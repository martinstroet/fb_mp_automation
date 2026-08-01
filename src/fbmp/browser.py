"""Persistent-profile Chrome via Playwright, with session/checkpoint detection
and human-ish page behavior (scrolls, mouse drift, dwell)."""

from __future__ import annotations

import logging
import random
from contextlib import contextmanager

from playwright.sync_api import BrowserContext, Page, sync_playwright

log = logging.getLogger(__name__)

MARKETPLACE_URL = "https://www.facebook.com/marketplace"


class SessionExpired(Exception):
    """We appear logged out — user must re-run `fbmp.main login`."""


class Checkpoint(Exception):
    """FB threw a checkpoint/security challenge — stand down immediately."""


@contextmanager
def open_browser(cfg, headed_override: bool = False):
    headless = bool(cfg.get("browser", "headless", default=True)) and not headed_override
    args = []
    if not headless and not headed_override:
        # headed fallback mode during scheduled runs: park the window offscreen
        args.append("--window-position=-2400,-2400")
    with sync_playwright() as p:
        ctx: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.profile_dir),
            channel=cfg.get("browser", "channel", default="chrome"),
            headless=headless,
            args=args,
            viewport={"width": random.randint(1280, 1600), "height": random.randint(800, 1000)},
            locale="en-AU",
            timezone_id="Australia/Brisbane",
        )
        try:
            yield ctx
        finally:
            ctx.close()


def check_session(page: Page):
    """Raise if the current page shows we're logged out or checkpointed."""
    url = page.url or ""
    if "/checkpoint" in url:
        raise Checkpoint(url)
    if "/login" in url or page.locator('input[name="pass"]').count() > 0:
        raise SessionExpired(url)


def goto(page: Page, url: str, pacer=None):
    """Navigate, verify session, and settle like a human would."""
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(random.uniform(1500, 3500))
    check_session(page)
    if pacer:
        drift_mouse(page)


def drift_mouse(page: Page):
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.move(random.randint(100, 1100), random.randint(120, 750), steps=random.randint(5, 20))
            page.wait_for_timeout(random.uniform(150, 700))
    except Exception:  # cosmetic only — never fail a cycle over mouse drift
        pass


def human_scroll(page: Page, flicks: int):
    for _ in range(flicks):
        page.mouse.wheel(0, random.randint(400, 1200))
        page.wait_for_timeout(random.uniform(600, 2200))
    if flicks and random.random() < 0.3:  # sometimes scroll back up a bit
        page.mouse.wheel(0, -random.randint(200, 600))
        page.wait_for_timeout(random.uniform(300, 900))


def probe_logged_in(page: Page) -> bool:
    """Cheap check used by `login` verification and session-recovery probes."""
    try:
        goto(page, MARKETPLACE_URL)
        return True
    except (SessionExpired, Checkpoint):
        return False
