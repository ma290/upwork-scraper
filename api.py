"""FastAPI REST API with continuous background search worker.
Optimized for 512MB RAM environments (e.g. Koyeb Free Tier).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import gc
import json
import logging
import os
import sqlite3
from typing import Any, Optional

import psutil
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.core.service import UpworkJobService
from src.schemas.input import (
    ActorInput,
    ExperienceLevel,
    JobType,
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

# Database setup
DB_PATH = os.getenv("DATABASE_PATH", "/app/storage/jobs.db")
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)


def init_db():
    """Initialize SQLite database for storing jobs and search logs."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
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

# Concurrency lock: Only 1 active browser at a time to strictly respect 512MB RAM
SCRAPE_LOCK = asyncio.Semaphore(1)

# Global Worker State
class WorkerConfig:
    enabled: bool = os.getenv("AUTORUN_SEARCH", "true").lower() == "true"
    keywords: list[str] = [
        k.strip() for k in os.getenv("SEARCH_KEYWORDS", "python, automation, web3").split(",") if k.strip()
    ]
    interval_seconds: int = int(os.getenv("SEARCH_INTERVAL_SECONDS", "300"))  # Default 5 minutes
    is_running: bool = False
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    total_runs: int = 0
    total_new_jobs_found: int = 0


worker_state = WorkerConfig()


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


def save_jobs_to_db(jobs: list[dict[str, Any]]) -> tuple[int, int]:
    """Save jobs to SQLite, returning (total_processed, new_jobs_inserted)."""
    if not jobs:
        return 0, 0

    new_count = 0
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
                new_count += 1
        conn.commit()
    return len(jobs), new_count


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
    """Execute a single low-memory scraping cycle and close Chrome immediately."""
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
            extract_details=False,  # Keep false to protect 512MB RAM
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
            # Force immediate browser destruction & garbage collection
            try:
                await service.cleanup()
            except Exception:
                pass
            del service
            gc.collect()
            logger.info("Finished scrape cycle for '%s'. Memory freed: %s", keyword, _get_memory_usage())

        # Normalize and save jobs
        formatted = [_format_job(j, keyword) for j in collected_raw_jobs]
        total_found, new_jobs = save_jobs_to_db(formatted)

        duration = (datetime.now() - start_time).total_seconds()
        with sqlite3.connect(DB_PATH) as conn:
            conn.cursor().execute(
                """
                INSERT INTO worker_logs (timestamp, keyword, jobs_found, new_jobs, duration_seconds, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (datetime.now(timezone.utc).isoformat(), keyword, total_found, new_jobs, duration, status_text, err_msg),
            )
            conn.commit()

        return total_found, new_jobs


async def continuous_worker_loop():
    """Background loop that continuously runs search cycles on the configured interval."""
    logger.info("Continuous search worker task started.")
    while True:
        try:
            if worker_state.enabled and worker_state.keywords:
                worker_state.is_running = True
                worker_state.last_run = datetime.now(timezone.utc).isoformat()

                for kw in list(worker_state.keywords):
                    if not worker_state.enabled:
                        break
                    _, new_jobs = await _run_scrape_cycle(kw)
                    worker_state.total_new_jobs_found += new_jobs
                    if new_jobs > 0:
                        logger.info("🔥 [NEW JOBS DETECTED] Found %d brand new jobs for '%s'!", new_jobs, kw)
                    # Short rest between keywords
                    await asyncio.sleep(5)

                worker_state.total_runs += 1
                worker_state.is_running = False
                worker_state.next_run = datetime.fromtimestamp(
                    datetime.now().timestamp() + worker_state.interval_seconds, timezone.utc
                ).isoformat()

            # Sleep during idle time — Chrome is closed, RAM is minimal (~40MB)
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
    # Start continuous background worker on startup
    worker_task = asyncio.create_task(continuous_worker_loop())
    yield
    # Cancel worker on shutdown
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Upwork Continuous Job Hunter API",
    version="2.0.0",
    description="Continuous background Upwork job search worker + REST API for 512MB servers.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Dashboard"])
async def dashboard():
    """Welcome dashboard with worker status and quick links."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs")
        total_in_db = cursor.fetchone()[0]

    return {
        "message": "Upwork Continuous Job Hunter is active",
        "worker_status": {
            "enabled": worker_state.enabled,
            "currently_scraping": worker_state.is_running,
            "keywords": worker_state.keywords,
            "interval_seconds": worker_state.interval_seconds,
            "last_run": worker_state.last_run,
            "next_run": worker_state.next_run,
            "total_runs": worker_state.total_runs,
            "total_jobs_in_db": total_in_db,
        },
        "endpoints": {
            "latest_jobs": "/api/jobs",
            "worker_status": "/api/worker/status",
            "worker_config": "/api/worker/config (POST)",
            "worker_toggle": "/api/worker/toggle (POST)",
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
    """Retrieve collected jobs from the database, sorted newest first."""
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

        # Parse skills JSON
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
        "keywords": worker_state.keywords,
        "interval_seconds": worker_state.interval_seconds,
        "last_run": worker_state.last_run,
        "next_run": worker_state.next_run,
        "total_runs": worker_state.total_runs,
        "total_new_jobs_found": worker_state.total_new_jobs_found,
        "recent_logs": logs,
        "memory": _get_memory_usage(),
    }


class WorkerUpdate(BaseModel):
    enabled: Optional[bool] = None
    keywords: Optional[list[str]] = Field(None, description="List of keywords to search continuously")
    interval_seconds: Optional[int] = Field(None, ge=60, le=86400, description="Seconds between runs (min 60s)")


@app.post("/api/worker/config", tags=["Worker"])
async def update_worker_config(cfg: WorkerUpdate):
    """Change keywords, search frequency, or enable/disable the worker on the fly."""
    if cfg.enabled is not None:
        worker_state.enabled = cfg.enabled
    if cfg.keywords is not None:
        worker_state.keywords = [k.strip() for k in cfg.keywords if k.strip()]
    if cfg.interval_seconds is not None:
        worker_state.interval_seconds = cfg.interval_seconds

    return {
        "message": "Worker configuration updated successfully",
        "current_config": {
            "enabled": worker_state.enabled,
            "keywords": worker_state.keywords,
            "interval_seconds": worker_state.interval_seconds,
        },
    }


@app.post("/api/worker/trigger", tags=["Worker"])
async def trigger_instant_search():
    """Force an immediate search run now without waiting for the interval."""
    if worker_state.is_running:
        raise HTTPException(status_code=400, detail="Worker is already performing a scrape cycle right now.")

    async def _run_all():
        for kw in list(worker_state.keywords):
            await _run_scrape_cycle(kw)
            await asyncio.sleep(3)

    asyncio.create_task(_run_all())
    return {"message": "Instant search triggered across all configured keywords in background."}
