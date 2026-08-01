"""SQLite state: listings, evaluations, alerts, digest queue, runs, kv."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  listing_id        TEXT PRIMARY KEY,
  first_seen_at     INTEGER NOT NULL,
  last_seen_at      INTEGER NOT NULL,
  title             TEXT,
  price_text        TEXT,
  price_aud         INTEGER,
  location          TEXT,
  url               TEXT,
  thumb_path        TEXT,
  source_target_id  TEXT,
  source_query      TEXT,
  detail_fetched_at INTEGER,
  description       TEXT,
  listed_ago_text   TEXT,
  seller_name       TEXT,
  seller_joined_year INTEGER,
  image_count       INTEGER,
  raw_card_json     TEXT,
  raw_detail_json   TEXT,
  sweep             TEXT NOT NULL DEFAULT 'fresh',   -- fresh | stale (discovery sweep type)
  status            TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);

CREATE TABLE IF NOT EXISTS evaluations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id  TEXT NOT NULL,
  stage       INTEGER NOT NULL,
  target_id   TEXT,
  matched     INTEGER,
  est_value_aud INTEGER,
  bargain_rating TEXT,
  meets_target INTEGER,
  hot         INTEGER,
  negotiation INTEGER DEFAULT 0,        -- worth-an-offer candidate
  suggested_offer_aud INTEGER,
  dubious     INTEGER,
  dubious_reasons TEXT,
  confidence  REAL,
  rationale   TEXT,
  raw_json    TEXT,
  created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id TEXT NOT NULL,
  kind       TEXT NOT NULL,          -- hot | digest
  sent_at    INTEGER,
  UNIQUE(listing_id, kind)
);

CREATE TABLE IF NOT EXISTS digest_queue (
  listing_id TEXT PRIMARY KEY REFERENCES listings(listing_id),
  target_id  TEXT,
  queued_at  INTEGER NOT NULL,
  sent_at    INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at    INTEGER NOT NULL,
  finished_at   INTEGER,
  kind          TEXT NOT NULL,       -- cycle | digest
  searches      INTEGER DEFAULT 0,
  cards_seen    INTEGER DEFAULT 0,
  new_listings  INTEGER DEFAULT 0,
  detail_fetches INTEGER DEFAULT 0,
  hot_alerts    INTEGER DEFAULT 0,
  errors        INTEGER DEFAULT 0,
  note          TEXT
);

CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """Add columns introduced after a DB was first created."""
        for table, col, decl in [
            ("listings", "sweep", "TEXT NOT NULL DEFAULT 'fresh'"),
            ("evaluations", "negotiation", "INTEGER DEFAULT 0"),
            ("evaluations", "suggested_offer_aud", "INTEGER"),
        ]:
            cols = [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def close(self):
        self.db.close()

    # -- kv ------------------------------------------------------------
    def kv_get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value):
        self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    # -- listings ------------------------------------------------------
    def upsert_card(self, card: dict) -> bool:
        """Insert a scraped card. Returns True if it was new."""
        now = int(time.time())
        cur = self.db.execute(
            """INSERT OR IGNORE INTO listings
               (listing_id, first_seen_at, last_seen_at, title, price_text, price_aud,
                location, url, thumb_path, source_target_id, source_query, sweep,
                raw_card_json, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
            (
                card["listing_id"], now, now, card.get("title"), card.get("price_text"),
                card.get("price_aud"), card.get("location"), card.get("url"),
                card.get("thumb_path"), card.get("source_target_id"),
                card.get("source_query"), card.get("sweep", "fresh"), json.dumps(card),
            ),
        )
        if cur.rowcount == 0:
            self.db.execute(
                "UPDATE listings SET last_seen_at=? WHERE listing_id=?",
                (now, card["listing_id"]),
            )
            self.db.commit()
            return False
        self.db.commit()
        return True

    def save_detail(self, listing_id: str, detail: dict):
        self.db.execute(
            """UPDATE listings SET detail_fetched_at=?, description=?, listed_ago_text=?,
               seller_name=?, seller_joined_year=?, image_count=?, raw_detail_json=?
               WHERE listing_id=?""",
            (
                int(time.time()), detail.get("description"), detail.get("listed_ago_text"),
                detail.get("seller_name"), detail.get("seller_joined_year"),
                detail.get("image_count"), json.dumps(detail), listing_id,
            ),
        )
        self.db.commit()

    def set_status(self, listing_id: str, status: str):
        self.db.execute("UPDATE listings SET status=? WHERE listing_id=?", (status, listing_id))
        self.db.commit()

    def listings_with_status(self, status: str, limit: int = 200) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM listings WHERE status=? ORDER BY first_seen_at LIMIT ?",
            (status, limit),
        ).fetchall()

    def shortlisted_for_detail(self, limit: int = 5) -> list[sqlite3.Row]:
        """Shortlisted listings, best stage-1 margin (est value - ask) first, so a
        backlog bigger than the per-cycle detail cap spends the budget on the
        most promising candidates. FIFO breaks ties."""
        return self.db.execute(
            """SELECT l.* FROM listings l WHERE l.status='shortlisted'
               ORDER BY COALESCE(
                   (SELECT e.est_value_aud FROM evaluations e
                    WHERE e.listing_id=l.listing_id AND e.stage=1
                    ORDER BY e.id DESC LIMIT 1) - l.price_aud, 0) DESC,
                 l.first_seen_at
               LIMIT ?""",
            (limit,),
        ).fetchall()

    def get_listing(self, listing_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM listings WHERE listing_id=?", (listing_id,)
        ).fetchone()

    # -- evaluations ---------------------------------------------------
    def record_evaluation(self, listing_id: str, stage: int, ev: dict):
        self.db.execute(
            """INSERT INTO evaluations
               (listing_id, stage, target_id, matched, est_value_aud, bargain_rating,
                meets_target, hot, negotiation, suggested_offer_aud, dubious,
                dubious_reasons, confidence, rationale, raw_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                listing_id, stage, ev.get("target_id"),
                int(bool(ev.get("matched"))), ev.get("est_value_aud"),
                ev.get("bargain_rating"), int(bool(ev.get("meets_target"))),
                int(bool(ev.get("hot"))), int(bool(ev.get("negotiation"))),
                ev.get("suggested_offer_aud"), int(bool(ev.get("dubious"))),
                json.dumps(ev.get("dubious_reasons") or []),
                ev.get("confidence"), ev.get("rationale"),
                json.dumps(ev), int(time.time()),
            ),
        )
        self.db.commit()

    def latest_evaluation(self, listing_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM evaluations WHERE listing_id=? ORDER BY id DESC LIMIT 1",
            (listing_id,),
        ).fetchone()

    # -- alerts / digest -----------------------------------------------
    def try_claim_alert(self, listing_id: str, kind: str) -> bool:
        """Atomically claim the right to send this alert. False if already claimed."""
        try:
            self.db.execute(
                "INSERT INTO alerts(listing_id, kind, sent_at) VALUES(?,?,?)",
                (listing_id, kind, int(time.time())),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def release_alerts(self, listing_ids: list[str], kind: str):
        """Roll back claims whose send failed, so the next attempt retries them."""
        self.db.executemany(
            "DELETE FROM alerts WHERE listing_id=? AND kind=?",
            [(lid, kind) for lid in listing_ids],
        )
        self.db.commit()

    def queue_digest(self, listing_id: str, target_id: str | None):
        self.db.execute(
            "INSERT OR IGNORE INTO digest_queue(listing_id, target_id, queued_at) VALUES(?,?,?)",
            (listing_id, target_id, int(time.time())),
        )
        self.db.commit()

    def pending_digest(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT q.listing_id, q.target_id, l.*
               FROM digest_queue q JOIN listings l ON l.listing_id = q.listing_id
               WHERE q.sent_at IS NULL ORDER BY q.target_id, q.queued_at"""
        ).fetchall()

    def mark_digest_sent(self, listing_ids: list[str]):
        now = int(time.time())
        self.db.executemany(
            "UPDATE digest_queue SET sent_at=? WHERE listing_id=?",
            [(now, lid) for lid in listing_ids],
        )
        self.db.commit()

    # -- runs ----------------------------------------------------------
    def start_run(self, kind: str, note: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO runs(started_at, kind, note) VALUES(?,?,?)",
            (int(time.time()), kind, note),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, **counters):
        sets = "".join(f", {k}=?" for k in counters)
        vals = list(counters.values())
        self.db.execute(
            f"UPDATE runs SET finished_at=?{sets} WHERE id=?",
            [int(time.time()), *vals, run_id],
        )
        self.db.commit()

    def runs_today(self) -> list[sqlite3.Row]:
        lt = time.localtime()
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        return self.db.execute(
            "SELECT * FROM runs WHERE started_at>=? ORDER BY id", (midnight,)
        ).fetchall()
