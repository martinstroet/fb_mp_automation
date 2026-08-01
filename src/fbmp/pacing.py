"""Humanization engine.

Every decision about *when* and *how much* to touch Facebook flows through
here, as a draw from configured ranges — never a fixed constant. The launchd
timer is only a wake-up tick; this module decides whether the wake becomes a
session at all, how long to idle first, and what shape the session takes.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import time


class Pacer:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    # -- gate: does this wake become a session? ------------------------
    def gate(self, now: dt.datetime | None = None) -> tuple[bool, str]:
        """Returns (run?, reason). Checked before any network traffic."""
        now = now or dt.datetime.now()
        hour = now.hour + now.minute / 60

        lo, hi = self.cfg.get("pacing", "active_hours", default=[6.5, 23.0])
        if not (lo <= hour < hi):
            return False, "outside_active_hours"

        gap_until = self.store.kv_get("long_gap_until", 0)
        if time.time() < gap_until:
            return False, "long_gap"

        # Maybe start a new "away from the phone" gap (~once a day).
        if random.random() < self.cfg.get("pacing", "long_gap_probability", default=0.03):
            lo_m, hi_m = self.cfg.get("pacing", "long_gap_minutes", default=[60, 90])
            self.store.kv_set("long_gap_until", time.time() + random.uniform(lo_m, hi_m) * 60)
            return False, "long_gap_started"

        weights = self.cfg.get("pacing", "hour_weights", default={}) or {}
        weight = float(weights.get(str(now.hour), weights.get("default", 0.8)))
        base_skip = self.cfg.get("pacing", "skip_probability_base", default=0.15)
        # weight 0.95 -> skip ~ base*0.5; weight 0.8 -> base*2; weight 0.6 -> base*4
        skip_p = min(0.9, base_skip * (1.0 - weight) / (1.0 - 0.9))
        if random.random() < skip_p:
            return False, "skipped_humanization"
        return True, "run"

    def pre_sleep_seconds(self) -> float:
        lo, hi = self.cfg.get("pacing", "pre_sleep_minutes", default=[0, 8])
        return random.uniform(lo * 60, hi * 60)

    # -- session shape -------------------------------------------------
    def searches_this_cycle(self) -> int:
        lo, hi = self.cfg.get("pacing", "searches_per_cycle", default=[2, 6])
        cap = self.cfg.get("limits", "max_searches_per_cycle", default=6)
        # triangular: mean sits between lo and hi, extremes rarer
        return min(cap, max(1, round(random.triangular(lo, hi, (lo + hi) / 2 + 0.5))))

    def use_organic_entry(self) -> bool:
        return random.random() < self.cfg.get("pacing", "organic_entry_probability", default=0.3)

    def do_decoy(self) -> bool:
        return random.random() < self.cfg.get("pacing", "decoy_probability", default=0.1)

    # -- delays --------------------------------------------------------
    def nav_delay(self) -> float:
        """Long-tailed delay between navigations (humans pause irregularly)."""
        lo, hi = self.cfg.get("pacing", "nav_delay_seconds", default=[4, 15])
        mu = math.log((lo + hi) / 2.5)
        d = random.lognormvariate(mu, 0.5)
        return max(lo, min(hi * 2.0, d))

    def dwell(self) -> float:
        lo, hi = self.cfg.get("pacing", "dwell_seconds", default=[5, 25])
        return random.uniform(lo, hi)

    def scroll_flicks(self) -> int:
        lo, hi = self.cfg.get("pacing", "scroll_flicks", default=[0, 3])
        return random.randint(lo, hi)

    @staticmethod
    def sleep(seconds: float):
        time.sleep(seconds)
