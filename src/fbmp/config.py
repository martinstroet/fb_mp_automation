"""Load and validate settings.yaml, targets.yaml and .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"

BARGAIN_LEVELS = ["market", "below_market", "well_below_market"]
# Ratings claude may return, ordered worst->best for comparison
RATING_ORDER = {
    "above_market": -1,
    "market": 0,
    "below_market": 1,
    "well_below_market": 2,
}


def rating_meets(rating: str, required_level: str) -> bool:
    """True if a listing's bargain rating is at-or-better-than the target's ask."""
    if rating not in RATING_ORDER or required_level not in RATING_ORDER:
        return False
    return RATING_ORDER[rating] >= RATING_ORDER[required_level]


@dataclass
class Target:
    id: str
    description: str
    bargain_level: str = "below_market"
    max_price: int | None = None
    notes: str = ""
    queries: list[str] = field(default_factory=list)
    active: bool = True
    email: str = ""  # per-target alert destination; empty = global ALERT_EMAIL


@dataclass
class Email:
    address: str
    app_password: str
    to: str


@dataclass
class Config:
    settings: dict
    targets: list[Target]
    email: Email | None
    dry_run: bool = False

    @property
    def db_path(self) -> Path:
        name = "fbmp.dryrun.db" if self.dry_run else "fbmp.db"
        return DATA_DIR / name

    @property
    def profile_dir(self) -> Path:
        return DATA_DIR / "browser_profile"

    @property
    def thumbs_dir(self) -> Path:
        return DATA_DIR / "thumbs"

    @property
    def debug_dir(self) -> Path:
        return DATA_DIR / "debug"

    @property
    def logs_dir(self) -> Path:
        return DATA_DIR / "logs"

    def active_targets(self) -> list[Target]:
        return [t for t in self.targets if t.active]

    def target_email(self, target_id: str | None) -> str | None:
        """Per-target alert destination override, or None for the global default.
        Looks at all targets (not just active) so queued items from a paused
        target still route to its address."""
        for t in self.targets:
            if t.id == target_id and t.email:
                return t.email
        return None

    def get(self, *keys, default=None):
        """settings.yaml lookup: cfg.get('pacing', 'nav_delay_seconds')."""
        node = self.settings
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


class ConfigError(Exception):
    pass


def load_targets(path: Path | None = None) -> list[Target]:
    path = path or CONFIG_DIR / "targets.yaml"
    raw = yaml.safe_load(path.read_text())
    if not raw or "targets" not in raw:
        raise ConfigError(f"{path}: missing top-level 'targets' list")
    defaults = raw.get("defaults") or {}
    targets, seen_ids = [], set()
    for i, entry in enumerate(raw["targets"]):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: target #{i + 1} is not a mapping")
        merged = {**defaults, **entry}
        tid = merged.get("id")
        if not tid:
            raise ConfigError(f"{path}: target #{i + 1} has no id")
        if tid in seen_ids:
            raise ConfigError(f"{path}: duplicate target id '{tid}'")
        seen_ids.add(tid)
        if not merged.get("description", "").strip():
            raise ConfigError(f"{path}: target '{tid}' has no description")
        level = merged.get("bargain_level", "below_market")
        if level not in BARGAIN_LEVELS:
            raise ConfigError(
                f"{path}: target '{tid}' bargain_level '{level}' not one of {BARGAIN_LEVELS}"
            )
        email = (merged.get("email") or "").strip()
        if email and "@" not in email:
            raise ConfigError(f"{path}: target '{tid}' email '{email}' is not an address")
        targets.append(
            Target(
                id=tid,
                description=merged["description"].strip(),
                bargain_level=level,
                max_price=merged.get("max_price"),
                notes=(merged.get("notes") or "").strip(),
                queries=list(merged.get("queries") or []),
                active=bool(merged.get("active", True)),
                email=email,
            )
        )
    return targets


def load_config(dry_run: bool = False, require_email: bool = True) -> Config:
    load_dotenv(REPO_ROOT / ".env")
    settings_path = CONFIG_DIR / "settings.yaml"
    if not settings_path.exists():
        raise ConfigError(f"missing {settings_path}")
    settings = yaml.safe_load(settings_path.read_text()) or {}

    email = None
    addr = os.environ.get("GMAIL_ADDRESS", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    to = os.environ.get("ALERT_EMAIL", addr)
    if addr and pw:
        email = Email(address=addr, app_password=pw, to=to)
    elif require_email and not dry_run:
        raise ConfigError(
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — copy .env.example to .env and fill it in"
        )

    for d in ("thumbs", "debug", "logs", "browser_profile"):
        (DATA_DIR / d).mkdir(parents=True, exist_ok=True)

    return Config(settings=settings, targets=load_targets(), email=email, dry_run=dry_run)
