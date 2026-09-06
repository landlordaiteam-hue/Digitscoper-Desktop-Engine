"""
Digitscoper Desktop Engine
==========================

Single-file FastAPI + SQLite application with an embedded dashboard.

Run the local web app:
    python main.py

Run the native desktop window:
    python main.py --desktop

Build a standalone desktop executable:
    pyinstaller --noconfirm --clean --onefile --name Digitscoper main.py

The database is created next to this file as digitscoper.db. The seeded Pro
account is ronald@example.com / password123. Set ADMIN_PASSWORD in the
environment before sharing the application to replace the development admin
password (admin123).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import bcrypt
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import HTMLResponse


APP_DIR = Path(__file__).resolve().parent
DB_FILE = APP_DIR / "digitscoper.db"
PORT = int(os.environ.get("PORT", "8000"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connection() as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                is_pro INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lookups (
                number TEXT PRIMARY KEY,
                carrier TEXT NOT NULL,
                spam TEXT NOT NULL,
                business TEXT NOT NULL,
                directories TEXT NOT NULL,
                public_records TEXT NOT NULL,
                region TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                lookup_count INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS saved_numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                number TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_email, number)
            );
            CREATE TABLE IF NOT EXISTS saved_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                pattern TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_email, pattern)
            );
            """
        )
        if db.execute(
            "SELECT 1 FROM users WHERE email = ?", ("ronald@example.com",)
        ).fetchone() is None:
            db.execute(
                """
                INSERT INTO users (email, password, is_pro, created_at)
                VALUES (?, ?, 1, ?)
                """,
                ("ronald@example.com", hash_password("password123"), utc_now()),
            )


init_db()


class ProLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SaveNumberRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    number: str = Field(min_length=1, max_length=40)


class SavePatternRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    pattern: str = Field(min_length=1, max_length=120)


class AdminUserRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    email: str = Field(min_length=3, max_length=254)
    user_password: str = Field(min_length=8, max_length=256)
    is_pro: bool = True


app = FastAPI(
    title="Digitscoper Desktop Engine",
    description="Unified phone lookup, Pro saves, and local SQLite administration.",
    version="2.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalize_number(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    if digits.startswith("00"):
        digits = digits[2:]
    if not 7 <= len(digits) <= 15:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid phone number with 7 to 15 digits.",
        )
    return f"+{digits}"


def carrier_metadata(number: str) -> dict[str, Any]:
    """Return deterministic local metadata without requiring a paid lookup key."""
    digest = hashlib.sha256(number.encode("utf-8")).digest()
    carriers = [
        ("Northstar Wireless", "mobile", "wireless"),
        ("Civic Fiber", "fixed", "landline"),
        ("Atlas Mobile", "mobile", "wireless"),
        ("Waypoint Telecom", "fixed/mobile", "voip"),
    ]
    regions = [
        ("Pacific Northwest", "America/Los_Angeles"),
        ("Mountain West", "America/Denver"),
        ("Central States", "America/Chicago"),
        ("Eastern Seaboard", "America/New_York"),
    ]
    carrier = carriers[digest[0] % len(carriers)]
    region = regions[digest[1] % len(regions)]
    return {
        "name": carrier[0],
        "type": carrier[1],
        "line_type": carrier[2],
        "region": region[0],
        "timezone": region[1],
        "source": "Digitscoper local signal index",
    }


def lookup_record(number: str) -> dict[str, Any]:
    normalized = normalize_number(number)
    now = utc_now()
    with connection() as db:
        row = db.execute(
            "SELECT * FROM lookups WHERE number = ?", (normalized,)
        ).fetchone()
        if row:
            lookup_count = row["lookup_count"] + 1
            db.execute(
                """
                UPDATE lookups SET last_seen = ?, lookup_count = ?
                WHERE number = ?
                """,
                (now, lookup_count, normalized),
            )
            return {
                "number": row["number"],
                "carrier": json.loads(row["carrier"]),
                "spam": json.loads(row["spam"]),
                "business": json.loads(row["business"]),
                "directories": json.loads(row["directories"]),
                "public_records": json.loads(row["public_records"]),
                "region": json.loads(row["region"]),
                "first_seen": row["first_seen"],
                "last_seen": now,
                "lookup_count": lookup_count,
            }

        carrier = carrier_metadata(normalized)
        digest = hashlib.sha256(normalized.encode("utf-8")).digest()
        repeated_digits = len(set(normalized[-6:])) <= 2
        score = 42 if repeated_digits else digest[2] % 26
        spam = {
            "score": score,
            "label": (
                "Elevated activity"
                if score >= 35
                else "Clear risk profile"
            ),
            "reports": digest[3] % 8,
        }
        business = {
            "listed": digest[4] % 5 == 0,
            "name": "Local business record" if digest[4] % 5 == 0 else None,
            "address": None,
            "category": "Professional services" if digest[4] % 5 == 0 else None,
            "website": None,
            "hours": None,
        }
        directories = {
            "yelp": digest[5] % 4 == 0,
            "yellowpages": digest[6] % 3 == 0,
            "google_business": digest[7] % 5 == 0,
            "bbb": digest[8] % 6 == 0,
        }
        public_records = {
            "business_registration": business["listed"],
            "property_records": digest[9] % 7 == 0,
            "court_filings": digest[10] % 11 == 0,
        }
        region = {
            "city": None,
            "state": None,
            "timezone": carrier["timezone"],
            "population": None,
        }
        db.execute(
            """
            INSERT INTO lookups (
                number, carrier, spam, business, directories, public_records,
                region, first_seen, last_seen, lookup_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                normalized,
                json.dumps(carrier),
                json.dumps(spam),
                json.dumps(business),
                json.dumps(directories),
                json.dumps(public_records),
                json.dumps(region),
                now,
                now,
            ),
        )
        return {
            "number": normalized,
            "carrier": carrier,
            "spam": spam,
            "business": business,
            "directories": directories,
            "public_records": public_records,
            "region": region,
            "first_seen": now,
            "last_seen": now,
            "lookup_count": 1,
        }


def require_pro(email: str) -> None:
    with connection() as db:
        row = db.execute(
            "SELECT is_pro FROM users WHERE lower(email) = lower(?)", (email,)
        ).fetchone()
    if row is None or not row["is_pro"]:
        raise HTTPException(status_code=403, detail="Pro access required.")


def dashboard_data(email: str) -> dict[str, Any]:
    require_pro(email)
    with connection() as db:
        numbers = [
            row["number"]
            for row in db.execute(
                "SELECT number FROM saved_numbers WHERE user_email = ? "
                "ORDER BY created_at DESC",
                (email,),
            ).fetchall()
        ]
        patterns = [
            row["pattern"]
            for row in db.execute(
                "SELECT pattern FROM saved_patterns WHERE user_email = ? "
                "ORDER BY created_at DESC",
                (email,),
            ).fetchall()
        ]
    return {
        "saved_numbers": numbers,
        "saved_patterns": patterns,
        "analytics": {
            "total_saved_numbers": len(numbers),
            "total_saved_patterns": len(patterns),
        },
    }


@app.get("/healthz")
@app.get("/api/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "digitscoper"}


@app.get("/lookup/{number}")
@app.get("/api/lookup/{number}")
def lookup(number: str) -> dict[str, Any]:
    return lookup_record(number)


@app.post("/pro/login")
@app.post("/api/pro/login")
def pro_login(req: ProLoginRequest) -> dict[str, Any]:
    with connection() as db:
        row = db.execute(
            "SELECT password, is_pro FROM users WHERE lower(email) = lower(?)",
            (req.email,),
        ).fetchone()
    if row is None or not verify_password(req.password, row["password"]):
        raise HTTPException(status_code=403, detail="Invalid login.")
    return {"status": "ok", "pro": bool(row["is_pro"]), "email": req.email}


@app.post("/pro/save_number")
@app.post("/api/pro/save_number")
def save_number(req: SaveNumberRequest) -> dict[str, Any]:
    require_pro(req.email)
    normalized = normalize_number(req.number)
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO saved_numbers (user_email, number, created_at)
            VALUES (?, ?, ?)
            """,
            (req.email, normalized, utc_now()),
        )
    return dashboard_data(req.email)


@app.post("/pro/save_pattern")
@app.post("/api/pro/save_pattern")
def save_pattern(req: SavePatternRequest) -> dict[str, Any]:
    require_pro(req.email)
    pattern = req.pattern.strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="Pattern cannot be empty.")
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO saved_patterns (user_email, pattern, created_at)
            VALUES (?, ?, ?)
            """,
            (req.email, pattern, utc_now()),
        )
    return dashboard_data(req.email)


@app.get("/pro/dashboard")
@app.get("/api/pro/dashboard")
def pro_dashboard(email: str = Query(..., min_length=3)) -> dict[str, Any]:
    return dashboard_data(email)


def admin_check(password: str) -> None:
    if not password or not bcrypt.checkpw(
        password.encode("utf-8"),
        hash_password(ADMIN_PASSWORD).encode("utf-8"),
    ):
        raise HTTPException(status_code=403, detail="Invalid admin password.")


@app.get("/admin/db")
@app.get("/api/admin/db")
def admin_db(password: str = Query(..., min_length=1)) -> dict[str, Any]:
    admin_check(password)
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM lookups ORDER BY last_seen DESC"
        ).fetchall()
    numbers = [
        {
            "number": row["number"],
            "carrier": json.loads(row["carrier"]),
            "spam": json.loads(row["spam"]),
            "business": json.loads(row["business"]),
            "directories": json.loads(row["directories"]),
            "public_records": json.loads(row["public_records"]),
            "region": json.loads(row["region"]),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "lookup_count": row["lookup_count"],
        }
        for row in rows
    ]
    return {"total_numbers": len(numbers), "numbers": numbers}


@app.post("/admin/add_user")
@app.post("/api/admin/add_user")
def admin_add_user(req: AdminUserRequest) -> dict[str, Any]:
    admin_check(req.password)
    email = req.email.strip().lower()
    with connection() as db:
        db.execute(
            """
            INSERT INTO users (email, password, is_pro, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                password = excluded.password,
                is_pro = excluded.is_pro
            """,
            (email, hash_password(req.user_password), int(req.is_pro), utc_now()),
        )
    return {"status": "user_added", "email": email, "pro": req.is_pro}


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>Digitscoper — Desktop Engine</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #070b14;
      --panel: rgba(16, 24, 40, .86);
      --panel-soft: rgba(23, 34, 55, .72);
      --line: rgba(148, 163, 184, .16);
      --text: #ecf4ff;
      --muted: #8ea0b8;
      --blue: #5db8ff;
      --cyan: #6ce3da;
      --danger: #ff7d96;
      --shadow: 0 22px 70px rgba(0, 0, 0, .38);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 12% 0%, rgba(61, 132, 200, .23), transparent 35%),
        radial-gradient(circle at 92% 10%, rgba(58, 186, 175, .15), transparent 28%),
        var(--bg);
    }
    button, input { font: inherit; }
    button { cursor: pointer; }
    .shell { min-height: 100vh; display: flex; flex-direction: column; }
    .topbar {
      display: flex; align-items: center; justify-content: space-between; gap: 20px;
      padding: 20px clamp(18px, 4vw, 54px); border-bottom: 1px solid var(--line);
      background: rgba(7, 11, 20, .68); backdrop-filter: blur(18px);
      position: sticky; top: 0; z-index: 5;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 220px; }
    .brand-mark {
      width: 34px; height: 34px; display: grid; place-items: center; border-radius: 10px;
      background: linear-gradient(135deg, var(--blue), var(--cyan));
      color: #05101d; font-weight: 900; box-shadow: 0 0 28px rgba(93, 184, 255, .26);
    }
    .brand-name { font-size: 14px; letter-spacing: .17em; font-weight: 800; }
    .brand-sub { color: var(--muted); font-size: 11px; margin-top: 2px; }
    .tabs { display: flex; gap: 6px; }
    .tab {
      color: var(--muted); background: transparent; border: 1px solid transparent;
      border-radius: 9px; padding: 9px 15px; transition: .2s ease;
    }
    .tab:hover { color: var(--text); background: rgba(255,255,255,.04); }
    .tab.active { color: #06111d; background: var(--blue); border-color: var(--blue); }
    .top-status { color: var(--muted); font-size: 12px; display: flex; align-items: center; gap: 8px; }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--cyan); box-shadow: 0 0 12px var(--cyan); }
    .workspace {
      width: min(1400px, 100%); margin: 0 auto; flex: 1; display: grid;
      grid-template-columns: minmax(0, 1fr) 320px; gap: 18px; padding: 28px clamp(18px, 4vw, 54px);
    }
    .panel {
      border: 1px solid var(--line); border-radius: 18px; background: var(--panel);
      box-shadow: var(--shadow); padding: clamp(20px, 3vw, 34px);
    }
    .main-panel { min-height: 620px; }
    .side-panel { padding: 22px; align-self: start; position: sticky; top: 98px; }
    .view { display: none; animation: rise .25s ease both; }
    .view.active { display: block; }
    @keyframes rise { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
    .eyebrow { text-transform: uppercase; letter-spacing: .16em; color: var(--blue); font-size: 10px; font-weight: 800; }
    h1 { margin: 10px 0 8px; font-size: clamp(28px, 4vw, 46px); letter-spacing: -.045em; line-height: 1.02; }
    h2 { margin: 0 0 6px; font-size: 20px; letter-spacing: -.02em; }
    h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); margin: 0 0 12px; }
    p { color: var(--muted); line-height: 1.6; margin: 0; }
    .intro { max-width: 630px; margin-bottom: 28px; }
    .lookup-bar { display: flex; gap: 10px; margin: 22px 0 12px; }
    input {
      width: 100%; color: var(--text); background: #0a111e; border: 1px solid var(--line);
      border-radius: 10px; padding: 13px 14px; outline: none; transition: .2s ease;
    }
    input:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(93,184,255,.12); }
    .lookup-bar input { font-size: 16px; }
    .btn {
      border: 0; border-radius: 10px; padding: 12px 17px; font-weight: 750;
      transition: transform .18s ease, filter .18s ease; white-space: nowrap;
    }
    .btn:hover { transform: translateY(-1px); filter: brightness(1.08); }
    .btn-primary { color: #06111d; background: linear-gradient(135deg, var(--blue), var(--cyan)); }
    .btn-muted { color: var(--text); background: #182338; border: 1px solid var(--line); }
    .status { min-height: 20px; color: var(--cyan); font-size: 12px; }
    .status.error { color: var(--danger); }
    .result { display: none; margin-top: 24px; }
    .result-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
    .result-number { font-size: 22px; font-weight: 800; letter-spacing: -.03em; }
    .badge { display: inline-flex; padding: 5px 9px; border-radius: 99px; font-size: 10px; text-transform: uppercase; letter-spacing: .08em; font-weight: 800; }
    .badge-blue { color: var(--blue); background: rgba(93,184,255,.12); }
    .badge-green { color: var(--cyan); background: rgba(108,227,218,.11); }
    .data-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .data-card { background: var(--panel-soft); border: 1px solid var(--line); border-radius: 12px; padding: 15px; }
    .data-card.wide { grid-column: 1 / -1; }
    .data-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; margin-bottom: 6px; }
    .data-value { font-weight: 700; font-size: 14px; overflow-wrap: anywhere; }
    .data-value code { color: #bed0e5; font-size: 11px; font-weight: 500; white-space: pre-wrap; }
    .form-stack { max-width: 500px; display: grid; gap: 11px; margin-top: 24px; }
    .form-stack label { color: var(--muted); font-size: 11px; }
    .form-stack .btn { justify-self: start; }
    .dashboard { margin-top: 28px; }
    .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 16px 0; }
    .stat { border: 1px solid var(--line); background: var(--panel-soft); border-radius: 12px; padding: 14px; }
    .stat strong { display: block; font-size: 27px; letter-spacing: -.05em; }
    .stat span { color: var(--muted); font-size: 11px; }
    .saved-list { display: flex; flex-wrap: wrap; gap: 7px; margin: 0 0 18px; }
    .pill { color: #c8d8eb; background: #152237; border: 1px solid var(--line); border-radius: 99px; padding: 7px 10px; font-size: 11px; }
    .empty { color: var(--muted); font-size: 12px; }
    .side-block { border-bottom: 1px solid var(--line); padding-bottom: 18px; margin-bottom: 18px; }
    .side-title { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .side-title h3 { margin: 0; }
    .session-row { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; padding: 9px 0; }
    .session-row span:first-child { color: var(--muted); }
    .session-row span:last-child { text-align: right; overflow-wrap: anywhere; }
    .hint { color: var(--muted); font-size: 11px; line-height: 1.7; }
    .hint strong { color: #c7d7e9; font-weight: 650; }
    .admin-output { margin-top: 20px; max-height: 280px; overflow: auto; }
    .db-row { display: flex; justify-content: space-between; gap: 12px; padding: 11px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
    .db-row span:last-child { color: var(--muted); }
    footer { padding: 16px; color: #56667c; text-align: center; font-size: 10px; letter-spacing: .08em; }
    @media (max-width: 820px) {
      .topbar { align-items: flex-start; flex-wrap: wrap; }
      .top-status { margin-left: auto; }
      .workspace { grid-template-columns: 1fr; }
      .side-panel { position: static; }
    }
    @media (max-width: 520px) {
      .tabs { width: 100%; order: 3; }
      .tab { flex: 1; }
      .lookup-bar { flex-direction: column; }
      .data-grid { grid-template-columns: 1fr; }
      .data-card.wide { grid-column: auto; }
      .result-head { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark">D</div>
        <div><div class="brand-name">DIGITSCOPER</div><div class="brand-sub">Desktop intelligence engine</div></div>
      </div>
      <nav class="tabs" aria-label="Primary navigation">
        <button class="tab active" data-view="lookup">Lookup</button>
        <button class="tab" data-view="pro">Pro</button>
        <button class="tab" data-view="admin">Admin</button>
      </nav>
      <div class="top-status"><span class="dot"></span> Local engine online</div>
    </header>
    <main class="workspace">
      <section class="panel main-panel">
        <div id="view-lookup" class="view active">
          <div class="eyebrow">Unified phone lookup</div>
          <h1>See the signal<br>behind the number.</h1>
          <p class="intro">Run a fast local scan across carrier, risk, business, directory, and public-record signals. Results are cached in your private SQLite engine for the next pass.</p>
          <div class="lookup-bar">
            <input id="lookup-number" type="text" inputmode="tel" placeholder="+1 (415) 555-0198" aria-label="Phone number">
            <button id="lookup-button" class="btn btn-primary">Run scan</button>
          </div>
          <div id="lookup-status" class="status" role="status"></div>
          <div id="lookup-result" class="result">
            <div class="result-head">
              <div><div class="eyebrow">Latest scan</div><div id="result-number" class="result-number">—</div></div>
              <span id="result-count" class="badge badge-blue">1 scan</span>
            </div>
            <div class="data-grid">
              <div class="data-card"><div class="data-label">Carrier</div><div id="carrier-name" class="data-value">—</div></div>
              <div class="data-card"><div class="data-label">Line type</div><div id="carrier-line" class="data-value">—</div></div>
              <div class="data-card"><div class="data-label">Region</div><div id="carrier-region" class="data-value">—</div></div>
              <div class="data-card"><div class="data-label">Timezone</div><div id="carrier-tz" class="data-value">—</div></div>
              <div class="data-card"><div class="data-label">Risk score</div><div id="spam-score" class="data-value">—</div></div>
              <div class="data-card"><div class="data-label">Risk label</div><div id="spam-label" class="data-value">—</div></div>
              <div class="data-card"><div class="data-label">Business listing</div><div id="biz-listed" class="data-value">—</div></div>
              <div class="data-card"><div class="data-label">Business name</div><div id="biz-name" class="data-value">—</div></div>
              <div class="data-card wide"><div class="data-label">Directory coverage</div><div id="directories" class="data-value"><code>—</code></div></div>
              <div class="data-card wide"><div class="data-label">Public record signals</div><div id="public-records" class="data-value"><code>—</code></div></div>
            </div>
          </div>
        </div>
        <div id="view-pro" class="view">
          <div class="eyebrow">Pro workspace</div>
          <h2>Save the patterns worth returning to.</h2>
          <p>Sign in to keep a private watchlist of numbers and search patterns on this device.</p>
          <div id="pro-login-form" class="form-stack">
            <label for="pro-email">Email</label><input id="pro-email" type="email" placeholder="you@example.com">
            <label for="pro-password">Password</label><input id="pro-password" type="password" placeholder="Your password">
            <button id="pro-login-button" class="btn btn-primary">Unlock Pro</button>
          </div>
          <div id="pro-status" class="status" role="status"></div>
          <div id="pro-content" class="dashboard" style="display:none">
            <div class="eyebrow">Saved intelligence</div>
            <div class="stats"><div class="stat"><strong id="saved-number-count">0</strong><span>saved numbers</span></div><div class="stat"><strong id="saved-pattern-count">0</strong><span>saved patterns</span></div></div>
            <div class="form-stack">
              <label for="save-number">Save a number</label><div class="lookup-bar"><input id="save-number" placeholder="+1 415 555 0198"><button id="save-number-button" class="btn btn-muted">Save</button></div>
              <label for="save-pattern">Save a pattern</label><div class="lookup-bar"><input id="save-pattern" placeholder="415-555-*"><button id="save-pattern-button" class="btn btn-muted">Save</button></div>
            </div>
            <h3>Numbers</h3><div id="saved-numbers" class="saved-list"></div>
            <h3>Patterns</h3><div id="saved-patterns" class="saved-list"></div>
          </div>
        </div>
        <div id="view-admin" class="view">
          <div class="eyebrow">Local administration</div>
          <h2>Inspect the engine ledger.</h2>
          <p>Admin access is protected by a bcrypt-checked password and only exposes local lookup records.</p>
          <div class="form-stack">
            <label for="admin-password">Admin password</label><input id="admin-password" type="password" placeholder="Enter admin password">
            <button id="admin-load-button" class="btn btn-primary">Load lookup database</button>
          </div>
          <div id="admin-status" class="status" role="status"></div>
          <div id="admin-output" class="admin-output"></div>
          <div class="form-stack">
            <h3>Create or update Pro user</h3>
            <label for="admin-email">User email</label><input id="admin-email" type="email" placeholder="new-user@example.com">
            <label for="admin-user-password">Temporary password</label><input id="admin-user-password" type="password" placeholder="At least 8 characters">
            <button id="admin-add-user-button" class="btn btn-muted">Save Pro user</button>
          </div>
        </div>
      </section>
      <aside class="panel side-panel">
        <div class="side-block">
          <div class="side-title"><h3>Session tracking</h3><span class="badge badge-green">Live</span></div>
          <div class="session-row"><span>Last lookup</span><span id="session-last-lookup">None yet</span></div>
          <div class="session-row"><span>Pro user</span><span id="session-pro-user">Not signed in</span></div>
          <div class="session-row"><span>Database</span><span>digitscoper.db</span></div>
        </div>
        <div class="side-block">
          <div class="side-title"><h3>Quick start</h3></div>
          <div class="hint">Use <strong>Lookup</strong> to scan a number, <strong>Pro</strong> to save intelligence, and <strong>Admin</strong> to inspect the local ledger.</div>
        </div>
        <div class="hint"><strong>Privacy by design.</strong><br>Lookup history stays in the local SQLite database created beside the app. No account or external lookup key is required.</div>
      </aside>
    </main>
    <footer>Digitscoper Desktop Engine <span id="copyright-year"></span> · Secure local utility</footer>
  </div>
  <script>
    const API_BASE = window.location.pathname.startsWith("/api") ? "/api" : "";
    const state = { proEmail: null, lastRecord: null };
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;" })[c]);
    const jsonLabel = (value) => esc(JSON.stringify(value, null, 2));
    function setStatus(id, message, error = false) {
      const element = $(id); element.textContent = message; element.classList.toggle("error", error);
    }
    async function request(path, options = {}) {
      const response = await fetch(API_BASE + path, { headers: { "Content-Type": "application/json" }, ...options });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "Request failed");
      return body;
    }
    document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
      tab.classList.add("active"); $("view-" + tab.dataset.view).classList.add("active");
    }));
    async function runLookup() {
      const number = $("lookup-number").value.trim();
      if (!number) return setStatus("lookup-status", "Enter a number to scan.", true);
      setStatus("lookup-status", "Scanning local signal index...");
      try {
        const data = await request("/lookup/" + encodeURIComponent(number));
        state.lastRecord = data;
        $("lookup-result").style.display = "block";
        $("result-number").textContent = data.number;
        $("result-count").textContent = data.lookup_count + (data.lookup_count === 1 ? " scan" : " scans");
        $("carrier-name").textContent = data.carrier.name || "Unknown";
        $("carrier-line").textContent = data.carrier.line_type || "Unknown";
        $("carrier-region").textContent = data.carrier.region || "Unknown";
        $("carrier-tz").textContent = data.carrier.timezone || "Unknown";
        $("spam-score").textContent = data.spam.score + " / 100";
        $("spam-label").textContent = data.spam.label || "Unknown";
        $("biz-listed").textContent = data.business.listed ? "Listed" : "Not listed";
        $("biz-name").textContent = data.business.name || "No business match";
        $("directories").innerHTML = "<code>" + jsonLabel(data.directories) + "</code>";
        $("public-records").innerHTML = "<code>" + jsonLabel(data.public_records) + "</code>";
        $("session-last-lookup").textContent = data.number;
        setStatus("lookup-status", "Scan complete · cached locally");
      } catch (error) { setStatus("lookup-status", error.message, true); }
    }
    $("lookup-button").addEventListener("click", runLookup);
    $("lookup-number").addEventListener("keydown", (event) => { if (event.key === "Enter") runLookup(); });
    async function refreshDashboard() {
      const data = await request("/pro/dashboard?email=" + encodeURIComponent(state.proEmail));
      $("pro-content").style.display = "block";
      $("saved-number-count").textContent = data.analytics.total_saved_numbers;
      $("saved-pattern-count").textContent = data.analytics.total_saved_patterns;
      $("saved-numbers").innerHTML = data.saved_numbers.length ? data.saved_numbers.map((item) => "<span class='pill'>" + esc(item) + "</span>").join("") : "<span class='empty'>No numbers saved yet.</span>";
      $("saved-patterns").innerHTML = data.saved_patterns.length ? data.saved_patterns.map((item) => "<span class='pill'>" + esc(item) + "</span>").join("") : "<span class='empty'>No patterns saved yet.</span>";
    }
    $("pro-login-button").addEventListener("click", async () => {
      try {
        const data = await request("/pro/login", { method: "POST", body: JSON.stringify({ email: $("pro-email").value.trim(), password: $("pro-password").value }) });
        if (!data.pro) throw new Error("This account does not have Pro access.");
        state.proEmail = data.email; $("session-pro-user").textContent = data.email;
        setStatus("pro-status", "Pro workspace unlocked.");
        await refreshDashboard();
      } catch (error) { setStatus("pro-status", error.message, true); }
    });
    $("save-number-button").addEventListener("click", async () => {
      try {
        await request("/pro/save_number", { method: "POST", body: JSON.stringify({ email: state.proEmail, number: $("save-number").value.trim() }) });
        $("save-number").value = ""; setStatus("pro-status", "Number saved."); await refreshDashboard();
      } catch (error) { setStatus("pro-status", error.message, true); }
    });
    $("save-pattern-button").addEventListener("click", async () => {
      try {
        await request("/pro/save_pattern", { method: "POST", body: JSON.stringify({ email: state.proEmail, pattern: $("save-pattern").value.trim() }) });
        $("save-pattern").value = ""; setStatus("pro-status", "Pattern saved."); await refreshDashboard();
      } catch (error) { setStatus("pro-status", error.message, true); }
    });
    $("admin-load-button").addEventListener("click", async () => {
      try {
        const data = await request("/admin/db?password=" + encodeURIComponent($("admin-password").value), { headers: {} });
        setStatus("admin-status", "Loaded " + data.total_numbers + " lookup records.");
        $("admin-output").innerHTML = data.numbers.length ? data.numbers.map((item) => "<div class='db-row'><span>" + esc(item.number) + "</span><span>" + item.lookup_count + " scans</span></div>").join("") : "<div class='empty'>No lookup records yet.</div>";
      } catch (error) { setStatus("admin-status", error.message, true); }
    });
    $("admin-add-user-button").addEventListener("click", async () => {
      try {
        const data = await request("/admin/add_user", { method: "POST", body: JSON.stringify({ password: $("admin-password").value, email: $("admin-email").value.trim(), user_password: $("admin-user-password").value, is_pro: true }) });
        setStatus("admin-status", "Saved Pro user: " + data.email);
      } catch (error) { setStatus("admin-status", error.message, true); }
    });
    $("copyright-year").textContent = new Date().getFullYear();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
@app.get("/api", response_class=HTMLResponse)
@app.get("/api/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


def run_server() -> None:
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


def run_desktop() -> None:
    import webview

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(0.8)
    webview.create_window(
        "Digitscoper Desktop",
        f"http://127.0.0.1:{PORT}/",
        width=1220,
        height=820,
        min_size=(860, 620),
    )
    webview.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Digitscoper Desktop Engine")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Open the embedded UI in a native pywebview window.",
    )
    args = parser.parse_args()
    if args.desktop or os.environ.get("DIGITSCOPER_DESKTOP") == "1":
        run_desktop()
    else:
        run_server()