"""SQLite persistence for Mise.

One database, one row per logical object. The whole client state is
reconstructed from these tables on GET /api/state.

Concurrency model: the `meta` table holds a monotonically increasing `rev`.
Any whole-state write must present the rev it read; a mismatch is a 409 and
the client refetches. Fine-grained writes (append a cook, adjust one pantry
item) bypass that check because they are inherently atomic — that is the
whole reason they exist as separate endpoints.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mise.db"
_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pantry (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    nl       TEXT DEFAULT '',
    cat      TEXT DEFAULT '',
    tag      TEXT DEFAULT '',
    cls      TEXT DEFAULT '',
    qty      REAL NOT NULL DEFAULT 0,
    unit     TEXT NOT NULL DEFAULT 'g',
    integer_ INTEGER NOT NULL DEFAULT 0,
    bought   TEXT,
    expires  TEXT,
    shelf    INTEGER DEFAULT 30,
    low      REAL DEFAULT 0,
    sub      TEXT DEFAULT '',
    price    REAL DEFAULT 0,
    pack     REAL DEFAULT 0,
    tare     REAL DEFAULT 0,      -- packaging tare, grams. see README
    barcode  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pantry_barcode ON pantry(barcode);

CREATE TABLE IF NOT EXISTS recipes (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    cuisine TEXT, meal TEXT, taste TEXT,
    mins    INTEGER DEFAULT 30,
    themes  TEXT DEFAULT '[]',    -- json array
    basis   TEXT,
    source  TEXT,
    stages  TEXT NOT NULL,        -- json array of stage objects
    builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cooks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    date      TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    mode      TEXT NOT NULL,
    theme     TEXT,
    factor    REAL DEFAULT 1,
    mae       REAL,
    total_ms  INTEGER DEFAULT 0,
    break_ms  INTEGER DEFAULT 0,
    payload   TEXT NOT NULL       -- json: actual, errors, classErr, rating, stageMs, subs
);
CREATE INDEX IF NOT EXISTS idx_cooks_recipe ON cooks(recipe_id);
CREATE INDEX IF NOT EXISTS idx_cooks_ts ON cooks(ts);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL              -- json
);

CREATE TABLE IF NOT EXISTS scale_samples (
    ts       INTEGER NOT NULL,
    cook_id  INTEGER,
    ingredient TEXT,
    grams    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scale_ts ON scale_samples(ts);
"""

DEFAULT_SETTINGS = {
    "reels": {"cuisine": "Any", "meal": "Any", "taste": "Any", "theme": "Any"},
    "pins": {"cuisine": False, "meal": False, "taste": False, "theme": False},
    "themeOther": "", "effort": "Any", "medium": "Any",
    "mode": "auto", "intakeMode": "manual",
    "basket": [], "intake": [],
    "appliances": [], "kit": {},
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, connect() as c:
        c.executescript(SCHEMA)
        cur = c.execute("SELECT v FROM meta WHERE k='rev'")
        if cur.fetchone() is None:
            c.execute("INSERT INTO meta(k,v) VALUES('rev','0')")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)", (k, json.dumps(v)))


def rev() -> int:
    with connect() as c:
        return int(c.execute("SELECT v FROM meta WHERE k='rev'").fetchone()["v"])


def bump() -> int:
    with _lock, connect() as c:
        n = int(c.execute("SELECT v FROM meta WHERE k='rev'").fetchone()["v"]) + 1
        c.execute("UPDATE meta SET v=? WHERE k='rev'", (str(n),))
        return n


# ── reads ───────────────────────────────────────────────────────────────────
def _pantry_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"], "nl": r["nl"], "cat": r["cat"], "tag": r["tag"],
        "cls": r["cls"], "qty": r["qty"], "unit": r["unit"], "integer": bool(r["integer_"]),
        "bought": r["bought"], "expires": r["expires"], "shelf": r["shelf"], "low": r["low"],
        "sub": r["sub"], "price": r["price"], "pack": r["pack"],
        "tare": r["tare"], "barcode": r["barcode"],
    }


def _recipe_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"], "cuisine": r["cuisine"], "meal": r["meal"],
        "taste": r["taste"], "mins": r["mins"], "themes": json.loads(r["themes"] or "[]"),
        "basis": r["basis"], "source": r["source"], "stages": json.loads(r["stages"]),
        "builtin": bool(r["builtin"]),
    }


def _cook_row(r: sqlite3.Row) -> dict[str, Any]:
    p = json.loads(r["payload"])
    return {
        "cookId": r["id"], "id": r["recipe_id"], "name": r["name"], "date": r["date"],
        "ts": r["ts"], "mode": r["mode"], "theme": r["theme"], "factor": r["factor"],
        "mae": r["mae"], "totalMs": r["total_ms"], "breakMs": r["break_ms"], **p,
    }


def get_state() -> dict[str, Any]:
    with connect() as c:
        pantry = {r["id"]: _pantry_row(r) for r in c.execute("SELECT * FROM pantry")}
        recipes = [_recipe_row(r) for r in c.execute("SELECT * FROM recipes")]
        log = [_cook_row(r) for r in c.execute("SELECT * FROM cooks ORDER BY ts ASC")]
        settings = {r["k"]: json.loads(r["v"]) for r in c.execute("SELECT * FROM settings")}
    return {
        "rev": rev(), "pantry": pantry, "log": log,
        "custom": [r for r in recipes if not r["builtin"]],
        "builtinOverrides": [r for r in recipes if r["builtin"]],
        **settings,
    }


# ── writes ──────────────────────────────────────────────────────────────────
def upsert_pantry(items: list[dict[str, Any]]) -> None:
    with _lock, connect() as c:
        for i in items:
            c.execute(
                """INSERT INTO pantry (id,name,nl,cat,tag,cls,qty,unit,integer_,bought,expires,
                                       shelf,low,sub,price,pack,tare,barcode)
                   VALUES (:id,:name,:nl,:cat,:tag,:cls,:qty,:unit,:integer_,:bought,:expires,
                           :shelf,:low,:sub,:price,:pack,:tare,:barcode)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, nl=excluded.nl, cat=excluded.cat, tag=excluded.tag,
                     cls=excluded.cls, qty=excluded.qty, unit=excluded.unit,
                     integer_=excluded.integer_, bought=excluded.bought, expires=excluded.expires,
                     shelf=excluded.shelf, low=excluded.low, sub=excluded.sub,
                     price=excluded.price, pack=excluded.pack, tare=excluded.tare,
                     barcode=excluded.barcode""",
                {
                    "id": i["id"], "name": i.get("name", i["id"]), "nl": i.get("nl", ""),
                    "cat": i.get("cat", ""), "tag": i.get("tag", ""), "cls": i.get("cls", ""),
                    "qty": float(i.get("qty", 0)), "unit": i.get("unit", "g"),
                    "integer_": 1 if i.get("integer") else 0,
                    "bought": i.get("bought"), "expires": i.get("expires"),
                    "shelf": int(i.get("shelf", 30)), "low": float(i.get("low", 0)),
                    "sub": i.get("sub", ""), "price": float(i.get("price", 0)),
                    "pack": float(i.get("pack", 0)), "tare": float(i.get("tare", 0)),
                    "barcode": i.get("barcode"),
                },
            )
    bump()


def adjust_qty(item_id: str, delta: float | None = None, absolute: float | None = None,
               bought: str | None = None, expires: str | None = None) -> dict[str, Any] | None:
    """Atomic single-item update. Used by the scale and the intake flow so a
    concurrent whole-state PUT can never clobber a decrement."""
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM pantry WHERE id=?", (item_id,)).fetchone()
        if row is None:
            return None
        qty = float(absolute) if absolute is not None else row["qty"] + float(delta or 0)
        qty = round(qty, 3)
        c.execute(
            "UPDATE pantry SET qty=?, bought=COALESCE(?,bought), expires=COALESCE(?,expires) WHERE id=?",
            (qty, bought, expires, item_id),
        )
        row = c.execute("SELECT * FROM pantry WHERE id=?", (item_id,)).fetchone()
        out = _pantry_row(row)
    bump()
    return out


def append_cook(cook: dict[str, Any]) -> int:
    """Atomic append. Never lost to a concurrent whole-state write."""
    payload = {k: cook.get(k) for k in
               ("actual", "errors", "classErr", "rating", "stageMs", "subs")}
    with _lock, connect() as c:
        cur = c.execute(
            """INSERT INTO cooks (recipe_id,name,date,ts,mode,theme,factor,mae,total_ms,break_ms,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (cook["id"], cook["name"], cook["date"], int(cook.get("ts", 0)),
             cook.get("mode", "manual"), cook.get("theme"), float(cook.get("factor", 1)),
             float(cook.get("mae", 0)), int(cook.get("totalMs", 0)),
             int(cook.get("breakMs", 0)), json.dumps(payload)),
        )
        cid = cur.lastrowid
    bump()
    return cid


def upsert_recipes(recipes: list[dict[str, Any]], builtin: bool = False) -> None:
    with _lock, connect() as c:
        for r in recipes:
            c.execute(
                """INSERT INTO recipes (id,name,cuisine,meal,taste,mins,themes,basis,source,stages,builtin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, cuisine=excluded.cuisine, meal=excluded.meal,
                     taste=excluded.taste, mins=excluded.mins, themes=excluded.themes,
                     basis=excluded.basis, source=excluded.source, stages=excluded.stages""",
                (r["id"], r["name"], r.get("cuisine"), r.get("meal"), r.get("taste"),
                 int(r.get("mins", 30)), json.dumps(r.get("themes", [])), r.get("basis"),
                 r.get("source"), json.dumps(r["stages"]), 1 if builtin else 0),
            )
    bump()


def delete_recipe(rid: str) -> None:
    with _lock, connect() as c:
        c.execute("DELETE FROM recipes WHERE id=? AND builtin=0", (rid,))
    bump()


def put_settings(settings: dict[str, Any]) -> None:
    with _lock, connect() as c:
        for k, v in settings.items():
            c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (k, json.dumps(v)))
    bump()


def log_scale(ts: int, grams: float, ingredient: str | None = None, cook_id: int | None = None) -> None:
    with connect() as c:
        c.execute("INSERT INTO scale_samples (ts,cook_id,ingredient,grams) VALUES (?,?,?,?)",
                  (ts, cook_id, ingredient, grams))


def find_by_barcode(code: str) -> dict[str, Any] | None:
    with connect() as c:
        r = c.execute("SELECT * FROM pantry WHERE barcode=?", (code,)).fetchone()
    return _pantry_row(r) if r else None
