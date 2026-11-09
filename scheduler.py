"""Persistent daily/elapsed-time scheduling, with no additional dependencies."""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time as clock_time
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from runtime import UserError, process_lock, write_json

logger = logging.getLogger("mywhoosh.scheduler")
UTC = timezone.utc


def utc_now():
    return datetime.now(UTC)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("An aware timestamp is required")
    return parsed.astimezone(UTC)


def positive_integer(env, name, default):
    try:
        value = int(env.get(name, default))
        if value <= 0:
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise UserError(f"{name} must be a positive integer") from None


@dataclass(frozen=True)
class Schedule:
    mode: str
    zone: ZoneInfo
    daily_time: time = time(20, 0)
    interval: int = 14400

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        mode = env.get("SCHEDULE_MODE", "daily")
        if mode not in {"daily", "interval"}:
            raise UserError("SCHEDULE_MODE must be daily or interval")
        try:
            zone = ZoneInfo(env.get("TZ", "Europe/Paris"))
        except (ZoneInfoNotFoundError, ValueError):
            raise UserError("TZ must be a valid IANA timezone, e.g. Europe/Paris") from None
        if mode == "daily":
            raw = env.get("SCHEDULE_TIME", "20:00")
            if not re.fullmatch(r"\d{2}:\d{2}", raw):
                raise UserError("SCHEDULE_TIME must use HH:MM, e.g. 20:00")
            try:
                daily = time.fromisoformat(raw)
            except ValueError:
                raise UserError("SCHEDULE_TIME must be a valid HH:MM time") from None
            return cls(mode, zone, daily_time=daily)
        raw = env.get("SCHEDULE_INTERVAL", "4h")
        match = re.fullmatch(r"([1-9]\d*)([smhd])", raw)
        if not match:
            raise UserError("SCHEDULE_INTERVAL must be a positive duration, e.g. 4h or 90m")
        seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        if seconds > 365 * 86400:
            raise UserError("SCHEDULE_INTERVAL must not exceed 365 days")
        return cls(mode, zone, interval=seconds)

    @property
    def identity(self):
        value = self.daily_time.isoformat() if self.mode == "daily" else str(self.interval)
        return f"{self.mode}:{value}:{self.zone.key}"

    def format_time(self, value):
        """Display local time explicitly; persistent timestamps remain UTC."""
        if isinstance(value, str):
            value = timestamp(value)
        local = value.astimezone(self.zone).isoformat(timespec="seconds")
        return f"{local} [{self.zone.key}]"

    def next_after(self, scheduled_for, now):
        """Skip missed slots rather than replaying every one of them."""
        now = now.astimezone(UTC)
        if self.mode == "interval":
            elapsed = max(0, (now - scheduled_for).total_seconds())
            return scheduled_for + timedelta(seconds=(int(elapsed // self.interval) + 1) * self.interval)
        day = now.astimezone(self.zone).date()
        for offset in range(3):
            # fold=0 selects the first occurrence of an ambiguous autumn time.
            # A nonexistent spring time is shifted forward by the DST gap.
            candidate = datetime.combine(day + timedelta(days=offset), self.daily_time,
                                         tzinfo=self.zone).astimezone(UTC)
            if candidate > now:
                return candidate
        raise UserError("Could not determine the next daily execution")


class Scheduler:
    def __init__(self, schedule, state_path, run_sync, *, clock=utc_now,
                 max_attempts=3, retry_delay=300):
        self.schedule = schedule
        self.state_path = Path(state_path)
        self.run_sync = run_sync
        self.clock = clock
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state["version"] != 1:
                raise ValueError
            timestamp(self.state["next_run"])
            timestamp(self.state["next_attempt"])
            if not isinstance(self.state["attempts"], int) or self.state["attempts"] < 0:
                raise ValueError
        except FileNotFoundError:
            self.state = {}
        except (ValueError, KeyError, TypeError):
            raise UserError("Invalid schedule.json; restore it or move it aside to reinitialise scheduling") from None
        if self.state.get("schedule") != schedule.identity:
            now = clock().isoformat()
            self.state = {"version": 1, "schedule": schedule.identity,
                          "next_run": now, "next_attempt": now, "attempts": 0}
            self.save()

    def save(self):
        write_json(self.state_path, self.state)

    def log_next_attempt(self):
        next_attempt = timestamp(self.state["next_attempt"])
        kind = "retry" if self.state["attempts"] else "scheduled sync"
        status = "due now" if next_attempt <= self.clock() else "waiting"
        logger.info("Next attempt: %s (%s; attempt %d/%d; %s).",
                    self.schedule.format_time(next_attempt), kind,
                    self.state["attempts"] + 1, self.max_attempts, status)

    def log_status(self):
        logger.info("Scheduler time: %s. Using persistent schedule state; "
                    "restarting does not force an extra sync.",
                    self.schedule.format_time(self.clock()))
        self.log_next_attempt()

    def tick(self):
        """Run at most one attempt. Retry counters survive container restarts."""
        now = self.clock()
        if now < timestamp(self.state["next_attempt"]):
            return False
        due = timestamp(self.state["next_run"])
        self.state["last_attempt"] = now.isoformat()
        self.save()  # Keep the slot due if interrupted while the sync is running.
        logger.info("Running sync for slot %s (started at %s; attempt %d/%d).",
                    self.schedule.format_time(due), self.schedule.format_time(now),
                    self.state["attempts"] + 1, self.max_attempts)
        code = self.run_sync()
        finished = self.clock()
        self.state["last_exit_code"] = code
        if code == 0:
            self.state["last_success"] = finished.isoformat()
        attempts = self.state["attempts"] + 1
        if code != 0 and attempts < self.max_attempts:
            self.state["attempts"] = attempts
            self.state["next_attempt"] = (finished + timedelta(seconds=self.retry_delay)).isoformat()
            logger.warning("Sync failed (exit %s); retry %d/%d in %d seconds.",
                           code, attempts + 1, self.max_attempts, self.retry_delay)
        else:
            if code != 0:
                logger.error("Sync failed after %d attempts; waiting for the next scheduled slot.", attempts)
            next_run = self.schedule.next_after(due, finished).isoformat()
            self.state.update(next_run=next_run, next_attempt=next_run, attempts=0)
        self.save()
        self.log_next_attempt()
        return True


def run_child(script, stopped, timeout):
    """Keep the parent responsive to SIGTERM and enforce a whole-sync timeout."""
    child = subprocess.Popen([sys.executable, str(script), "--once", "--non-interactive"])
    deadline = clock_time.monotonic() + timeout
    try:
        while child.poll() is None:
            if stopped.wait(0.5):
                raise InterruptedError
            if clock_time.monotonic() >= deadline:
                logger.error("Sync exceeded SYNC_TIMEOUT_SECONDS; terminating it.")
                return 124
        return child.returncode
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def serve(data_dir, script, handlers=None):
    schedule = Schedule.from_env()
    attempts = positive_integer(os.environ, "SYNC_MAX_ATTEMPTS", 3)
    delay = positive_integer(os.environ, "SYNC_RETRY_DELAY_SECONDS", 300)
    timeout = positive_integer(os.environ, "SYNC_TIMEOUT_SECONDS", 1800)
    logger.handlers = list(handlers or [])
    logger.setLevel(logging.INFO)
    logger.propagate = False
    stopped = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        with process_lock(data_dir / "scheduler.lock"):
            scheduler = Scheduler(schedule, data_dir / "schedule.json",
                                  lambda: run_child(script, stopped, timeout),
                                  max_attempts=attempts, retry_delay=delay)
            logger.info("Scheduler started (%s).", schedule.identity)
            scheduler.log_status()
            while not stopped.is_set():
                scheduler.tick()
                stopped.wait(1)
    except InterruptedError:
        logger.info("Stopping; the interrupted slot remains due for restart.")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
