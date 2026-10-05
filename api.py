"""FastAPI REST API with continuous background search worker,
Telegram bot deep-linking, and user-specific job notifications.
Optimized for 512MB RAM environments (e.g. Android Termux PRoot & Koyeb).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import gc
import html
import json
import logging
import os
import sqlite3
from typing import Any, Optional

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.core.service import UpworkJobService
from src.schemas.input import (
    ActorInput,
    LocationFilter,
    SearchParameters,
    SortOrder,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("upwork_api")

# Configuration
DB_PATH = os.getenv("DATABASE_PATH", "/app/storage/jobs.db")
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "Upworkaelertbot")


def init_db():
    """Initialize SQLite database for storing jobs, users, and notifications."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Jobs table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                url TEXT,
                amount TEXT,
                hourly_rate TEXT,
                job_type TEXT,
                skills TEXT,
                published_on TEXT,
                client_location TEXT,
                client_payment_verified INTEGER,
                client_rating REAL,
                client_total_spent TEXT,
                found_at TEXT,
                keyword TEXT
            )
            """
        )
        # Users table (registered via Frontend Firebase Google Auth)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT,
                name TEXT,
                telegram_chat_id TEXT,
                keywords TEXT,
                min_budget INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                last_active TEXT
            )
            """
        )
        # Notifications tracking table (prevents sending duplicate alerts to a user)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                job_id TEXT,
                sent_at TEXT,
                UNIQUE(user_id, job_id)
            )
            """
        )
        # Worker execution logs
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                keyword TEXT,
                jobs_found INTEGER,
                new_jobs INTEGER,
                duration_seconds REAL,
                status TEXT,
                error TEXT
            )
            """
        )
        conn.commit()


init_db()

# Concurrency lock: Only 1 active browser at a time to strictly protect 512MB RAM
SCRAPE_LOCK = asyncio.Semaphore(1)


# Global Worker State
class WorkerConfig:
    enabled: bool = os.getenv("AUTORUN_SEARCH", "true").lower() == "true"
    default_keywords: list[str] = [
        k.strip() for k in os.getenv("SEARCH_KEYWORDS", "python, automation, web3, solidity").split(",") if k.strip()
    ]
    interval_seconds: int = int(os.getenv("SEARCH_INTERVAL_SECONDS", "300"))  # Default 5 minutes
    is_running: bool = False
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    total_runs: int = 0
    total_new_jobs_found: int = 0


worker_state = WorkerConfig()


# ============================================================================
# Telegram Notification Helpers
# ============================================================================

async def send_telegram_message(chat_id: str | int, text_html: str) -> bool:
    """Send an HTML-formatted message to a Telegram chat."""
    if not TELEGRAM_BOT_TOKEN:
        logger.debug("TELEGRAM_BOT_TOKEN not configured; skipping Telegram message.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            logger.warning("Telegram send failed (status %d): %s", resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.error("Error sending Telegram message: %s", exc)
        return False


def build_job_notification_html(job: dict[str, Any], matched_keyword: str) -> str:
    """Construct an attractive, clean HTML notification message for Telegram."""
    title = html.escape(job.get("title", "Upwork Job"))
    url = job.get("url", "https://www.upwork.com")
    budget = html.escape(str(job.get("amount") or job.get("hourly_rate") or "Not specified"))
    job_type = html.escape(str(job.get("job_type", "Fixed / Hourly")).capitalize())
    client_loc = html.escape(str(job.get("client_location") or "Unknown"))
    client_verified = "Verified ✅" if job.get("client_payment_verified") else "Unverified ⚠️"
    rating = f"{job['client_rating']} ⭐" if job.get("client_rating") else "No rating"
    spent = f" (${job.get('client_total_spent')} spent)" if job.get("client_total_spent") else ""

    skills_raw = job.get("skills", [])
    if isinstance(skills_raw, str):
        try:
            skills_raw = json.loads(skills_raw)
        except Exception:
            skills_raw = [skills_raw]
    skills_text = ", ".join(html.escape(s) for s in skills_raw[:6]) if skills_raw else "None specified"

    desc = job.get("description", "")
    desc_clean = html.escape(desc[:280] + ("..." if len(desc) > 280 else ""))

    return (
        f"🚀 <b>New Upwork Job Match!</b> (<i>{html.escape(matched_keyword)}</i>)\n\n"
        f"📌 <b><a href=\"{url}\">{title}</a></b>\n\n"
        f"💰 <b>Budget:</b> {budget} ({job_type})\n"
        f"🌍 <b>Client:</b> {client_loc} | {client_verified}\n"
        f"⭐ <b>Client Rating:</b> {rating}{spent}\n"
        f"🏷 <b>Skills:</b> {skills_text}\n\n"
        f"📝 <b>Summary:</b>\n{desc_clean}\n\n"
        f"👉 <a href=\"{url}\"><b>Open & Apply on Upwork</b></a>"
    )


async def dispatch_user_notifications(new_jobs: list[dict[str, Any]]):
    """Check each new job against all registered users' keywords and dispatch Telegram alerts."""
    if not TELEGRAM_BOT_TOKEN or not new_jobs:
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, name, telegram_chat_id, keywords, min_budget
            FROM users
            WHERE telegram_chat_id IS NOT NULL AND is_active = 1
            """
        )
        active_users = [dict(r) for r in cursor.fetchall()]

    if not active_users:
        logger.debug("No active users with connected Telegram accounts.")
        return

    for job in new_jobs:
        job_id = job.get("id")
        if not job_id:
            continue

        job_text = f"{job.get('title', '')} {job.get('description', '')} {job.get('skills', '')}".lower()

        for user in active_users:
            user_id = user["user_id"]
            chat_id = user["telegram_chat_id"]

            user_keywords = []
            if user.get("keywords"):
                try:
                    user_keywords = json.loads(user["keywords"])
                except Exception:
                    user_keywords = [k.strip() for k in user["keywords"].split(",") if k.strip()]

            # If user has no specific keywords, match on the scrape keyword
            if not user_keywords and job.get("keyword"):
                user_keywords = [job["keyword"]]

            # Check if any user keyword matches this job
            matched_kw = None
            for kw in user_keywords:
                if kw.lower() in job_text:
                    matched_kw = kw
                    break

            if matched_kw:
                # Check if already sent
                with sqlite3.connect(DB_PATH) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT id FROM notifications WHERE user_id = ? AND job_id = ?",
                        (user_id, job_id),
                    )
                    already_sent = cur.fetchone() is not None

                if not already_sent:
                    msg = build_job_notification_html(job, matched_kw)
                    success = await send_telegram_message(chat_id, msg)
                    if success:
                        logger.info("Sent Telegram alert for job '%s' to user %s", job.get("title"), user_id)
                        with sqlite3.connect(DB_PATH) as conn:
                            cur = conn.cursor()
                            cur.execute(
                                "INSERT OR IGNORE INTO notifications (user_id, job_id, sent_at) VALUES (?, ?, ?)",
                                (user_id, job_id, datetime.now(timezone.utc).isoformat()),
                            )
                            conn.commit()


# ============================================================================
# Telegram Bot Polling Loop (Handles /start <user_id> Deep Linking)
# ============================================================================

async def telegram_bot_poll_loop():
    """Background polling loop for Telegram updates to link users via /start <user_id>."""
    if not TELEGRAM_BOT_TOKEN:
        logger.info("Telegram Bot Token not set; Telegram Bot polling is disabled.")
        return

    logger.info("Starting Telegram Bot poller for deep-linking (/start <user_id>)...")
    offset = 0

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 20}

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params)

                if resp.status_code == 200:
                    data = resp.json()
                    updates = data.get("result", [])

                    for upd in updates:
                        offset = upd["update_id"] + 1
                        msg = upd.get("message")
                        if not msg:
                            continue

                        chat_id = str(msg["chat"]["id"])
                        text = msg.get("text", "").strip()
                        sender_name = msg.get("from", {}).get("first_name", "Friend")

                        if text.startswith("/start"):
                            # Extract deep-link parameter: /start <user_id>
                            parts = text.split(" ")
                            if len(parts) > 1 and parts[1].strip():
                                user_id = parts[1].strip()

                                # Link this Telegram chat_id to the user in SQLite
                                with sqlite3.connect(DB_PATH) as conn:
                                    conn.row_factory = sqlite3.Row
                                    cur = conn.cursor()
                                    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                                    user_row = cur.fetchone()

                                    if user_row:
                                        cur.execute(
                                            """
                                            UPDATE users
                                            SET telegram_chat_id = ?, is_active = 1, last_active = ?
                                            WHERE user_id = ?
                                            """,
                                            (chat_id, datetime.now(timezone.utc).isoformat(), user_id),
                                        )
                                        conn.commit()
                                        user_name = user_row["name"] or sender_name
                                        kw_display = user_row["keywords"] or "Default keywords"
                                    else:
                                        # Create new record if not yet synced from frontend
                                        cur.execute(
                                            """
                                            INSERT INTO users (user_id, name, telegram_chat_id, keywords, created_at, last_active)
                                            VALUES (?, ?, ?, ?, ?, ?)
                                            """,
                                            (
                                                user_id,
                                                sender_name,
                                                chat_id,
                                                json.dumps(worker_state.default_keywords),
                                                datetime.now(timezone.utc).isoformat(),
                                                datetime.now(timezone.utc).isoformat(),
                                            ),
                                        )
                                        conn.commit()
                                        user_name = sender_name
                                        kw_display = ", ".join(worker_state.default_keywords)

                                reply = (
                                    f"🎉 <b>Welcome {html.escape(user_name)}!</b>\n\n"
                                    f"✅ Your Telegram is now connected to <b>Upwork Job Alerts</b>!\n\n"
                                    f"🎯 <b>Your Active Keywords:</b> {html.escape(kw_display)}\n\n"
                                    f"Whenever a new matching job is found on Upwork, you will receive an instant notification here with direct apply links.\n\n"
                                    f"<i>Commands:</i>\n"
                                    f"/status - View your settings & alerts\n"
                                    f"/stop - Pause notifications"
                                )
                                await send_telegram_message(chat_id, reply)
                            else:
                                reply = (
                                    f"👋 Hello {html.escape(sender_name)}!\n\n"
                                    f"To connect your account, please click the <b>'Connect Telegram'</b> button on your Upwork Dashboard web app."
                                )
                                await send_telegram_message(chat_id, reply)

                        elif text == "/status":
                            with sqlite3.connect(DB_PATH) as conn:
                                conn.row_factory = sqlite3.Row
                                cur = conn.cursor()
                                cur.execute("SELECT * FROM users WHERE telegram_chat_id = ?", (chat_id,))
                                u = cur.fetchone()
                                if u:
                                    cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ?", (u["user_id"],))
                                    notif_count = cur.fetchone()[0]

                                    status_reply = (
                                        f"📊 <b>Your Alert Status:</b>\n\n"
                                        f"👤 <b>Name:</b> {html.escape(u['name'] or 'User')}\n"
                                        f"🟢 <b>Status:</b> {'Active' if u['is_active'] else 'Paused'}\n"
                                        f"🎯 <b>Keywords:</b> {html.escape(u['keywords'] or 'None')}\n"
                                        f"📬 <b>Total Alerts Received:</b> {notif_count}\n"
                                    )
                                else:
                                    status_reply = "⚠️ Account not found. Please connect through your web dashboard."
                            await send_telegram_message(chat_id, status_reply)

                        elif text == "/stop":
                            with sqlite3.connect(DB_PATH) as conn:
                                conn.cursor().execute("UPDATE users SET is_active = 0 WHERE telegram_chat_id = ?", (chat_id,))
                                conn.commit()
                            await send_telegram_message(chat_id, "⏸ <b>Notifications paused.</b> Type /start to resume.")

        except asyncio.CancelledError:
            logger.info("Telegram polling cancelled.")
            break
        except Exception as poll_err:
            logger.error("Error in Telegram poller: %s", poll_err)
            await asyncio.sleep(5)


# ============================================================================
# Job Formatting & Database Operations
# ============================================================================

def _format_job(job: dict[str, Any], search_keyword: str = "") -> dict[str, Any]:
    """Format and normalize raw scraped job dictionary."""
    ciphertext = job.get("ciphertext") or job.get("id") or job.get("uid") or ""
    url = f"https://www.upwork.com/jobs/{ciphertext}" if ciphertext else job.get("url", "")

    skills: list[str] = []
    if "attrs" in job and isinstance(job["attrs"], list):
        for attr in job["attrs"]:
            if isinstance(attr, dict) and "prettyName" in attr:
                skills.append(attr["prettyName"])
            elif isinstance(attr, str):
                skills.append(attr)
    elif "skills" in job and isinstance(job["skills"], list):
        skills = [str(s) for s in job["skills"]]

    client_loc = ""
    client_verified = 0
    client_rating = None
    client_spent = ""

    if "client" in job and isinstance(job["client"], dict):
        raw_client = job["client"]
        client_verified = 1 if (raw_client.get("paymentVerificationStatus") == 1 or raw_client.get("paymentVerified", False)) else 0
        loc = raw_client.get("location")
        client_loc = loc.get("country", "") if isinstance(loc, dict) else str(loc or "")
        client_rating = raw_client.get("rating")
        client_spent = str(raw_client.get("totalSpent") or "")

    amount_str = None
    if "amount" in job:
        if isinstance(job["amount"], dict):
            amount_str = f"{job['amount'].get('currency', 'USD')} {job['amount'].get('amount', '')}".strip()
        else:
            amount_str = str(job["amount"])

    return {
        "id": ciphertext,
        "title": job.get("title", "Untitled"),
        "description": job.get("description", ""),
        "url": url,
        "amount": amount_str,
        "hourly_rate": job.get("hourlyRate") or job.get("hourly_rate"),
        "job_type": job.get("jobType") or ("hourly" if "hourly" in str(job.get("type", "")).lower() else "fixed"),
        "skills": json.dumps(skills),
        "published_on": str(job.get("publishedOn") or job.get("publishTime") or job.get("createdOn") or ""),
        "client_location": client_loc,
        "client_payment_verified": client_verified,
        "client_rating": client_rating,
        "client_total_spent": client_spent,
        "found_at": datetime.now(timezone.utc).isoformat(),
        "keyword": search_keyword,
    }


def save_jobs_to_db(jobs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Save jobs to SQLite, returning (new_jobs_list, total_count)."""
    if not jobs:
        return [], 0

    new_jobs: list[dict[str, Any]] = []
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for j in jobs:
            if not j["id"]:
                continue
            cursor.execute("SELECT id FROM jobs WHERE id = ?", (j["id"],))
            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    INSERT INTO jobs (
                        id, title, description, url, amount, hourly_rate, job_type,
                        skills, published_on, client_location, client_payment_verified,
                        client_rating, client_total_spent, found_at, keyword
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        j["id"], j["title"], j["description"], j["url"], j["amount"],
                        j["hourly_rate"], j["job_type"], j["skills"], j["published_on"],
                        j["client_location"], j["client_payment_verified"],
                        j["client_rating"], j["client_total_spent"], j["found_at"], j["keyword"]
                    ),
                )
                new_jobs.append(j)
        conn.commit()

    return new_jobs, len(jobs)


def _get_memory_usage() -> dict[str, float]:
    """Get current RAM usage in MB."""
    process = psutil.Process(os.getpid())
    process_mb = process.memory_info().rss / (1024 * 1024)
    virtual_mem = psutil.virtual_memory()
    return {
        "api_process_mb": round(process_mb, 2),
        "system_used_mb": round(virtual_mem.used / (1024 * 1024), 2),
        "system_total_mb": round(virtual_mem.total / (1024 * 1024), 2),
        "system_percent": virtual_mem.percent,
    }


async def _run_scrape_cycle(keyword: str, max_jobs: int = 15) -> tuple[int, int]:
    """Execute a single low-memory scraping cycle and dispatch alerts."""
    async with SCRAPE_LOCK:
        logger.info("Starting scrape cycle for keyword: '%s' (RAM: %s)", keyword, _get_memory_usage())
        start_time = datetime.now()

        search_params = SearchParameters(
            keywords=keyword,
            sort_by=SortOrder.RECENCY,
            location=LocationFilter.WORLDWIDE,
        )

        actor_input = ActorInput(
            search_parameters=search_params,
            max_jobs=max_jobs,
            extract_details=False,
            debug_mode=False,
            delay_min=1.0,
            delay_max=2.5,
        )

        service = UpworkJobService(actor_input)
        collected_raw_jobs: list[dict[str, Any]] = []
        status_text = "success"
        err_msg = ""

        try:
            search_urls = actor_input.build_search_urls()
            await asyncio.wait_for(service.run_scraping(search_urls), timeout=120.0)
            collected_raw_jobs = getattr(service, "collected_jobs", [])
        except Exception as exc:
            status_text = "error"
            err_msg = str(exc)
            logger.error("Scraper cycle failed for '%s': %s", keyword, exc)
        finally:
            try:
                await service.cleanup()
            except Exception:
                pass
            del service
            gc.collect()
            logger.info("Finished scrape cycle for '%s'. Memory freed: %s", keyword, _get_memory_usage())

        # Normalize and save jobs
        formatted = [_format_job(j, keyword) for j in collected_raw_jobs]
        new_jobs, total_found = save_jobs_to_db(formatted)

        # Dispatch Telegram notifications to subscribed users
        if new_jobs:
            logger.info("Found %d brand new jobs for '%s'! Dispatching alerts...", len(new_jobs), keyword)
            await dispatch_user_notifications(new_jobs)

        duration = (datetime.now() - start_time).total_seconds()
        with sqlite3.connect(DB_PATH) as conn:
            conn.cursor().execute(
                """
                INSERT INTO worker_logs (timestamp, keyword, jobs_found, new_jobs, duration_seconds, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (datetime.now(timezone.utc).isoformat(), keyword, total_found, len(new_jobs), duration, status_text, err_msg),
            )
            conn.commit()

        return total_found, len(new_jobs)


def _get_active_keywords_to_scrape() -> list[str]:
    """Compile distinct keywords from all registered active users + default list."""
    keywords_set = set(worker_state.default_keywords)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT keywords FROM users WHERE is_active = 1 AND keywords IS NOT NULL")
        for row in cur.fetchall():
            try:
                user_kws = json.loads(row["keywords"])
                if isinstance(user_kws, list):
                    for kw in user_kws:
                        if kw and isinstance(kw, str) and kw.strip():
                            keywords_set.add(kw.strip().lower())
            except Exception:
                pass

    return list(keywords_set)


async def continuous_worker_loop():
    """Background loop that continuously runs search cycles for all user keywords."""
    logger.info("Continuous search worker loop started.")
    while True:
        try:
            if worker_state.enabled:
                worker_state.is_running = True
                worker_state.last_run = datetime.now(timezone.utc).isoformat()

                keywords_to_scrape = _get_active_keywords_to_scrape()
                logger.info("Worker scraping active keyword set (%d terms): %s", len(keywords_to_scrape), keywords_to_scrape)

                for kw in keywords_to_scrape:
                    if not worker_state.enabled:
                        break
                    _, new_count = await _run_scrape_cycle(kw)
                    worker_state.total_new_jobs_found += new_count
                    await asyncio.sleep(5)  # Rest between keywords

                worker_state.total_runs += 1
                worker_state.is_running = False
                worker_state.next_run = datetime.fromtimestamp(
                    datetime.now().timestamp() + worker_state.interval_seconds, timezone.utc
                ).isoformat()

            await asyncio.sleep(worker_state.interval_seconds)

        except asyncio.CancelledError:
            logger.info("Worker loop cancelled.")
            break
        except Exception as e:
            logger.error("Worker loop exception: %s", e, exc_info=True)
            worker_state.is_running = False
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start continuous scraper worker
    worker_task = asyncio.create_task(continuous_worker_loop())
    # Start Telegram bot polling
    telegram_task = asyncio.create_task(telegram_bot_poll_loop())
    yield
    worker_task.cancel()
    telegram_task.cancel()
    try:
        await asyncio.gather(worker_task, telegram_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Upwork Continuous Job Hunter & Telegram Alert API",
    version="3.0.0",
    description="Automated Upwork scraper with Telegram deep-linking and multi-user notifications for Termux / Koyeb.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# API Endpoints for Frontend (Vercel) & Users
# ============================================================================

class UserSyncRequest(BaseModel):
    user_id: str = Field(..., description="Firebase UID from Google Auth")
    email: Optional[str] = Field(None, description="User's Google email")
    name: Optional[str] = Field(None, description="User's display name")
    keywords: Optional[list[str]] = Field(default=["python", "web3"], description="List of alert keywords")
    min_budget: Optional[int] = Field(0, description="Minimum budget filter")


@app.post("/api/users/sync", tags=["Users"])
async def sync_user(data: UserSyncRequest):
    """
    Called by Frontend after Google Auth.
    Upserts user record and returns the personalized Telegram Deep Link.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (data.user_id,))
        existing = cur.fetchone()

        now = datetime.now(timezone.utc).isoformat()
        kws_json = json.dumps([k.strip().lower() for k in data.keywords if k.strip()]) if data.keywords else "[]"

        if existing:
            cur.execute(
                """
                UPDATE users
                SET email = COALESCE(?, email),
                    name = COALESCE(?, name),
                    keywords = ?,
                    min_budget = ?,
                    last_active = ?
                WHERE user_id = ?
                """,
                (data.email, data.name, kws_json, data.min_budget, now, data.user_id),
            )
            chat_id = existing["telegram_chat_id"]
        else:
            cur.execute(
                """
                INSERT INTO users (user_id, email, name, keywords, min_budget, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (data.user_id, data.email, data.name, kws_json, data.min_budget, now, now),
            )
            chat_id = None
        conn.commit()

    telegram_link = (
        f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={data.user_id}"
        if TELEGRAM_BOT_USERNAME
        else None
    )

    return {
        "user_id": data.user_id,
        "telegram_connected": bool(chat_id),
        "telegram_chat_id": chat_id,
        "telegram_link": telegram_link,
        "keywords": data.keywords,
    }


@app.get("/api/users/{user_id}", tags=["Users"])
async def get_user_profile(user_id: str):
    """Get user alert status, connected Telegram info, and keywords."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cur.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ?", (user_id,))
        alert_count = cur.fetchone()[0]

    telegram_link = (
        f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={user_id}"
        if TELEGRAM_BOT_USERNAME
        else None
    )

    kws = []
    if user["keywords"]:
        try:
            kws = json.loads(user["keywords"])
        except Exception:
            kws = [user["keywords"]]

    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "telegram_connected": bool(user["telegram_chat_id"]),
        "telegram_chat_id": user["telegram_chat_id"],
        "telegram_link": telegram_link,
        "keywords": kws,
        "min_budget": user["min_budget"],
        "is_active": bool(user["is_active"]),
        "total_alerts_received": alert_count,
    }


@app.get("/api/telegram/config", tags=["Telegram"])
async def get_telegram_config():
    """Return public Telegram Bot info for the frontend."""
    return {
        "bot_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME),
        "bot_username": TELEGRAM_BOT_USERNAME,
    }


@app.get("/", tags=["Dashboard"])
async def root():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs")
        total_jobs = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

    return {
        "message": "Upwork Job Hunter & Telegram Alert API",
        "stats": {
            "total_jobs_scraped": total_jobs,
            "registered_users": total_users,
            "worker_active": worker_state.enabled,
            "currently_scraping": worker_state.is_running,
            "telegram_bot_configured": bool(TELEGRAM_BOT_TOKEN),
        },
        "endpoints": {
            "jobs": "/api/jobs",
            "sync_user": "/api/users/sync (POST)",
            "worker_status": "/api/worker/status",
            "health": "/health",
            "docs": "/docs",
        },
        "memory": _get_memory_usage(),
    }


@app.get("/health", tags=["Dashboard"])
async def health():
    return {
        "status": "healthy",
        "worker_running": worker_state.is_running,
        "memory": _get_memory_usage(),
    }


@app.get("/api/jobs", tags=["Jobs"])
async def get_jobs(
    keyword: Optional[str] = Query(None, description="Filter by keyword"),
    search: Optional[str] = Query(None, description="Search text in title or description"),
    limit: int = Query(25, ge=1, le=100, description="Max jobs to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """Retrieve collected jobs from the database, newest first."""
    query = "SELECT * FROM jobs WHERE 1=1"
    params: list[Any] = []

    if keyword:
        query += " AND keyword = ?"
        params.append(keyword)

    if search:
        query += " AND (title LIKE ? OR description LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY found_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = [dict(r) for r in cursor.fetchall()]

        for r in rows:
            try:
                r["skills"] = json.loads(r["skills"]) if r.get("skills") else []
            except Exception:
                pass

        cursor.execute("SELECT COUNT(*) FROM jobs")
        total_count = cursor.fetchone()[0]

    return {
        "total_in_database": total_count,
        "returned": len(rows),
        "limit": limit,
        "offset": offset,
        "jobs": rows,
    }


@app.get("/api/worker/status", tags=["Worker"])
async def get_worker_status():
    """Get the current background worker status and recent crawl history."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worker_logs ORDER BY timestamp DESC LIMIT 10")
        logs = [dict(r) for r in cursor.fetchall()]

    return {
        "enabled": worker_state.enabled,
        "is_currently_scraping": worker_state.is_running,
        "active_keywords": _get_active_keywords_to_scrape(),
        "interval_seconds": worker_state.interval_seconds,
        "last_run": worker_state.last_run,
        "next_run": worker_state.next_run,
        "total_runs": worker_state.total_runs,
        "total_new_jobs_found": worker_state.total_new_jobs_found,
        "recent_logs": logs,
        "memory": _get_memory_usage(),
    }
