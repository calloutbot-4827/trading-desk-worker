"""SQLite (WAL) schema and helpers."""
import sqlite3, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins(mint TEXT PRIMARY KEY,name TEXT,symbol TEXT,pool TEXT,migrated_at REAL,creator TEXT,creator_balance_at_migration REAL,holders INTEGER);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY,mint TEXT,ts REAL,liquidity_usd REAL,top10_pct REAL,h1_change_pct REAL,age_min REAL,verdict TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY,mint TEXT,ts REAL,layer TEXT,name TEXT,value_text TEXT,pass INTEGER,source TEXT);
CREATE TABLE IF NOT EXISTS verdicts(id INTEGER PRIMARY KEY,mint TEXT,ts REAL,alive_6h REAL,organic REAL,flush REAL,chase REAL,score REAL,pass INTEGER,reason TEXT,input_tokens INTEGER);
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY,mint TEXT,ts REAL,price REAL,size_usd REAL,stack_pct REAL,link TEXT,mode TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY,mint TEXT,opened_at REAL,entry_price REAL,size_usd REAL,peak_price REAL,stop_price REAL,trail_active INTEGER DEFAULT 0,closed_at REAL,exit_price REAL,exit_reason TEXT,pnl_usd REAL,fees_usd REAL,mode TEXT);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY,ts REAL,kind TEXT CHECK(kind IN ('bought','sold','stopped','rejected')),mint TEXT,agent TEXT,title TEXT,subtitle TEXT,value TEXT);
CREATE TABLE IF NOT EXISTS prices(mint TEXT,ts REAL,price REAL,volume_m5 REAL);
CREATE INDEX IF NOT EXISTS prices_mint_ts ON prices(mint,ts);
CREATE TABLE IF NOT EXISTS balance(ts REAL,value REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT,updated_at REAL);
CREATE TABLE IF NOT EXISTS change_log(ts REAL,key TEXT,old TEXT,new TEXT,who TEXT);
CREATE TABLE IF NOT EXISTS skips(mint TEXT PRIMARY KEY,until REAL);
CREATE TABLE IF NOT EXISTS heartbeats(agent TEXT PRIMARY KEY,ts REAL);
CREATE TABLE IF NOT EXISTS push_subs(endpoint TEXT PRIMARY KEY,p256dh TEXT,auth TEXT,created_at REAL);
"""


def connect(path="desk.db"):
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(positions)")}
    if "stop_pct" not in cols:  # Phase 2: stop pct each position carries
        c.execute("ALTER TABLE positions ADD COLUMN stop_pct REAL")
    c.commit()
    return c


def insert(c, table, **row):
    cols = ",".join(row)
    cur = c.execute(f"INSERT INTO {table}({cols}) VALUES({','.join('?'*len(row))})", tuple(row.values()))
    c.commit()
    return cur.lastrowid


def upsert_coin(c, mint, **kw):
    row = {"mint": mint, **kw}
    cols = ",".join(row); upd = ",".join(f"{k}=excluded.{k}" for k in kw) or "mint=mint"
    c.execute(f"INSERT INTO coins({cols}) VALUES({','.join('?'*len(row))}) ON CONFLICT(mint) DO UPDATE SET {upd}", tuple(row.values()))
    c.commit()


def activity(c, kind, mint, agent, title, subtitle="", value=""):
    return insert(c, "activity", ts=time.time(), kind=kind, mint=mint, agent=agent, title=title, subtitle=subtitle, value=value)


def heartbeat(c, agent):
    c.execute("INSERT INTO heartbeats(agent,ts) VALUES(?,?) ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts", (agent, time.time()))
    c.commit()


def skip(c, mint, seconds):
    c.execute("INSERT INTO skips(mint,until) VALUES(?,?) ON CONFLICT(mint) DO UPDATE SET until=excluded.until", (mint, time.time() + seconds))
    c.commit()


def is_skipped(c, mint):
    r = c.execute("SELECT until FROM skips WHERE mint=?", (mint,)).fetchone()
    return bool(r and r[0] > time.time())


def open_positions(c):
    return c.execute("SELECT * FROM positions WHERE closed_at IS NULL").fetchall()


def latest_balance(c, default=None):
    r = c.execute("SELECT value FROM balance ORDER BY ts DESC LIMIT 1").fetchone()
    return r[0] if r else default
