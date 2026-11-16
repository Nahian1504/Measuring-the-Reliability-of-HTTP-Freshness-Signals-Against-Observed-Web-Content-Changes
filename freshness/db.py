from __future__ import annotations
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS site (
    site_id        INTEGER PRIMARY KEY,
    domain         TEXT UNIQUE NOT NULL,
    tranco_rank    INTEGER,
    rank_group     TEXT,
    robots_ok      INTEGER,
    robots_txt     TEXT,
    homepage_url   TEXT,
    status         TEXT,          
    exclude_reason TEXT,
    checked_at     REAL
);

CREATE TABLE IF NOT EXISTS resource (
    resource_id   INTEGER PRIMARY KEY,
    site_id       INTEGER NOT NULL REFERENCES site(site_id),
    url           TEXT UNIQUE NOT NULL,
    type          TEXT NOT NULL,   
    fingerprinted INTEGER NOT NULL DEFAULT 0,
    cond_sample   INTEGER NOT NULL DEFAULT 0,
    b2b_sample    INTEGER NOT NULL DEFAULT 0,
    hourly_sample INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    inactive_reason TEXT
);

CREATE TABLE IF NOT EXISTS round (
    round_id     INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,   
    started_at   REAL NOT NULL,
    finished_at  REAL,
    n_ok         INTEGER,
    n_err        INTEGER
);

CREATE TABLE IF NOT EXISTS page_text (
    text_sha256  TEXT PRIMARY KEY,
    main_text    BLOB,            
    minhash      BLOB,            
    n_words      INTEGER
);

CREATE TABLE IF NOT EXISTS fetch (
    fetch_id      INTEGER PRIMARY KEY,
    resource_id   INTEGER NOT NULL REFERENCES resource(resource_id),
    round_id      INTEGER NOT NULL REFERENCES round(round_id),
    kind          TEXT NOT NULL,   
    fetched_at    REAL NOT NULL,   
    status        INTEGER,
    error         TEXT,
    final_url     TEXT,
    n_redirects   INTEGER,
    attempts      INTEGER,
    etag          TEXT,
    etag_weak     INTEGER,
    last_modified TEXT,
    date_hdr      TEXT,
    cache_control TEXT,
    expires       TEXT,
    lifetime_s    REAL,
    lifetime_src  TEXT,
    lm_uninformative INTEGER,
    content_type  TEXT,
    content_encoding TEXT,
    size          INTEGER,
    body_sha256   TEXT,
    text_sha256   TEXT REFERENCES page_text(text_sha256),
    cdn_headers   TEXT,           
    bot_challenge INTEGER DEFAULT 0,
    cond_status   INTEGER,         
    cond_error    TEXT,
    elapsed_ms    INTEGER,
    UNIQUE (resource_id, round_id, kind)
);
CREATE INDEX IF NOT EXISTS fetch_res_time ON fetch(resource_id, kind, fetched_at);

CREATE TABLE IF NOT EXISTS fetch_pair (
    pair_id      INTEGER PRIMARY KEY,
    prev_fetch   INTEGER NOT NULL REFERENCES fetch(fetch_id),
    next_fetch   INTEGER NOT NULL REFERENCES fetch(fetch_id),
    resource_id  INTEGER NOT NULL,
    gap_s        REAL,
    B            INTEGER,          -- body hash changed
    M            INTEGER,          -- meaningful change (at theta)
    E            INTEGER,          -- ETag changed (NULL if missing)
    L            INTEGER,          -- Last-Modified changed (NULL if missing)
    etag_present INTEGER,
    etag_weak    INTEGER,
    similarity   REAL,
    early_change INTEGER,          -- changed while declared fresh (NULL if not fresh the whole gap)
    category     TEXT,
    UNIQUE (prev_fetch, next_fetch)
);
"""


def connect(path: str) -> sqlite3.Connection:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con
