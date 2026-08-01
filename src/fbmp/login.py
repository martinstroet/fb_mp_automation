"""One-time (re-runnable) interactive login bootstrap.

Opens headed Chrome on the persistent profile; the user logs in manually and
sets their Marketplace location/radius, then presses Enter here. We then verify
the session actually persists by reloading /marketplace.
"""

from __future__ import annotations

import logging

from .browser import MARKETPLACE_URL, open_browser, probe_logged_in

log = logging.getLogger(__name__)


def run_login(cfg) -> bool:
    print(
        "\nA Chrome window will open on facebook.com.\n"
        "  1. Log in (dismiss any save-password / notification prompts)\n"
        "  2. Open Marketplace and set your location + search radius\n"
        "  3. Come back here and press Enter\n"
    )
    with open_browser(cfg, headed_override=True) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        input("Press Enter once you are logged in and Marketplace location is set... ")
        page.goto(MARKETPLACE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        ok = probe_logged_in(page)

    if not ok:
        print("✗ Still looks logged out — run this again and complete the login.")
        return False

    # Re-open fresh to prove the session survives a browser restart.
    with open_browser(cfg, headed_override=True) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ok = probe_logged_in(page)

    print("✓ Logged-in session persisted." if ok else "✗ Session did not persist across restart.")
    return ok
