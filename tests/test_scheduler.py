import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from runtime import UserError
from scheduler import Schedule, Scheduler, run_child

UTC = timezone.utc


class ScheduleTests(unittest.TestCase):
    def test_display_converts_utc_to_paris_winter_time(self):
        schedule = Schedule.from_env({})
        self.assertEqual(schedule.format_time("2026-11-09T20:05:42+00:00"),
                         "2026-11-09T21:05:42+01:00 [Europe/Paris]")

    def test_display_converts_utc_to_paris_summer_time(self):
        schedule = Schedule.from_env({})
        self.assertEqual(schedule.format_time(datetime(2026, 7, 9, 20, 5, tzinfo=UTC)),
                         "2026-07-09T22:05:00+02:00 [Europe/Paris]")

    def test_default_is_daily_at_20_paris(self):
        schedule = Schedule.from_env({})
        now = datetime(2026, 11, 9, 12, tzinfo=UTC)
        self.assertEqual(schedule.next_after(now, now), datetime(2026, 11, 9, 19, tzinfo=UTC))

    def test_daily_next_day_after_slot(self):
        schedule = Schedule.from_env({})
        now = datetime(2026, 11, 9, 19, tzinfo=UTC)
        self.assertEqual(schedule.next_after(now, now), now + timedelta(days=1))

    def test_daily_tracks_dst_instead_of_adding_24_hours(self):
        schedule = Schedule.from_env({})
        now = datetime(2026, 3, 28, 19, tzinfo=UTC)
        self.assertEqual(schedule.next_after(now, now), datetime(2026, 3, 29, 18, tzinfo=UTC))

    def test_ambiguous_autumn_slot_runs_once(self):
        schedule = Schedule.from_env({"SCHEDULE_TIME": "02:30"})
        now = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        self.assertEqual(schedule.next_after(now, now), datetime(2026, 10, 26, 1, 30, tzinfo=UTC))

    def test_nonexistent_spring_slot_moves_forward(self):
        schedule = Schedule.from_env({"SCHEDULE_TIME": "02:30"})
        now = datetime(2026, 3, 28, 12, tzinfo=UTC)
        self.assertEqual(schedule.next_after(now, now), datetime(2026, 3, 29, 1, 30, tzinfo=UTC))

    def test_interval_is_elapsed_time_across_dst(self):
        schedule = Schedule.from_env({"SCHEDULE_MODE": "interval", "SCHEDULE_INTERVAL": "4h"})
        due = datetime(2026, 3, 29, 0, tzinfo=UTC)
        self.assertEqual(schedule.next_after(due, due), due + timedelta(hours=4))
        self.assertEqual(schedule.next_after(due, due + timedelta(hours=13)), due + timedelta(hours=16))

    def test_custom_minutes(self):
        schedule = Schedule.from_env({"SCHEDULE_MODE": "interval", "SCHEDULE_INTERVAL": "90m"})
        self.assertEqual(schedule.interval, 5400)

    def test_invalid_config_is_rejected(self):
        for env in ({"SCHEDULE_MODE": "cron"}, {"SCHEDULE_TIME": "25:00"},
                    {"SCHEDULE_TIME": "8:00"}, {"TZ": "not/a-zone"},
                    {"SCHEDULE_MODE": "interval", "SCHEDULE_INTERVAL": "0h"},
                    {"SCHEDULE_MODE": "interval", "SCHEDULE_INTERVAL": "500d"}):
            with self.subTest(env=env), self.assertRaises(UserError):
                Schedule.from_env(env)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "schedule.json"
        self.now = datetime(2026, 11, 9, 12, tzinfo=UTC)
        self.run = Mock(return_value=0)
        self.schedule = Schedule.from_env({})

    def scheduler(self, **kwargs):
        return Scheduler(self.schedule, self.path, self.run,
                         clock=lambda: self.now, **kwargs)

    def test_first_start_runs_immediately_then_waits_for_20(self):
        scheduler = self.scheduler()
        self.assertTrue(scheduler.tick())
        self.assertFalse(scheduler.tick())
        self.run.assert_called_once()
        self.assertEqual(scheduler.state["next_run"], "2026-11-09T19:00:00+00:00")
        self.assertIn("last_success", json.loads(self.path.read_text()))

    def test_restart_before_next_slot_does_not_sync(self):
        self.scheduler().tick()
        self.now += timedelta(hours=1)
        self.assertFalse(self.scheduler().tick())
        self.run.assert_called_once()

    def test_retry_is_relative_to_completion_not_daily_slot(self):
        # The 20:00 Paris slot is only executed at 20:57, finishing at 20:58.
        # A five-minute retry must therefore be at 21:03, not at 20:05.
        self.now = datetime(2026, 11, 9, 19, tzinfo=UTC)
        scheduler = self.scheduler(retry_delay=300)
        self.now += timedelta(minutes=57)
        def fail_after_one_minute():
            self.now += timedelta(minutes=1)
            return 1
        self.run.side_effect = fail_after_one_minute
        with self.assertLogs("mywhoosh.scheduler", level="INFO") as logs:
            scheduler.tick()
        self.assertEqual(scheduler.state["next_attempt"], "2026-11-09T20:03:00+00:00")
        self.assertIn("Next attempt: 2026-11-09T21:03:00+01:00 [Europe/Paris]",
                      "\n".join(logs.output))
        self.assertIn("started at 2026-11-09T20:57:00+01:00", "\n".join(logs.output))

    def test_restart_reports_pending_retry_without_forcing_it(self):
        self.now = datetime(2026, 11, 9, 19, 55, tzinfo=UTC)
        self.run.return_value = 1
        self.scheduler(retry_delay=600).tick()
        self.now += timedelta(minutes=2)
        scheduler = self.scheduler(retry_delay=600)
        before = self.path.read_text()
        with self.assertLogs("mywhoosh.scheduler", level="INFO") as logs:
            scheduler.log_status()
        self.assertFalse(scheduler.tick())
        self.run.assert_called_once()
        self.assertEqual(self.path.read_text(), before)
        output = "\n".join(logs.output)
        self.assertIn("Scheduler time: 2026-11-09T20:57:00+01:00", output)
        self.assertIn("Next attempt: 2026-11-09T21:05:00+01:00 [Europe/Paris]", output)
        self.assertIn("retry; attempt 2/3; waiting", output)
        self.assertIn("restarting does not force an extra sync", output)

    def test_startup_reports_overdue_attempt_in_local_time(self):
        scheduler = self.scheduler()
        self.now += timedelta(minutes=10)
        with self.assertLogs("mywhoosh.scheduler", level="INFO") as logs:
            scheduler.log_status()
        self.assertIn("due now", "\n".join(logs.output))
        self.assertIn("[Europe/Paris]", "\n".join(logs.output))
        self.run.assert_not_called()

    def test_restart_after_many_missed_slots_catches_up_once(self):
        self.scheduler().tick()
        self.now += timedelta(days=5)
        scheduler = self.scheduler()
        self.assertTrue(scheduler.tick())
        self.assertFalse(scheduler.tick())
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(scheduler.state["next_run"], "2026-11-14T19:00:00+00:00")

    def test_retries_are_bounded_and_persist_across_restart(self):
        self.run.return_value = 1
        scheduler = self.scheduler(max_attempts=3, retry_delay=300)
        scheduler.tick()
        self.assertEqual(scheduler.state["attempts"], 1)
        self.assertFalse(self.scheduler().tick())
        self.now += timedelta(minutes=5)
        self.scheduler().tick()
        self.now += timedelta(minutes=5)
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(scheduler.state["attempts"], 0)
        self.assertNotIn("last_success", scheduler.state)
        self.assertFalse(self.scheduler().tick())

    def test_success_on_retry_clears_attempt_counter(self):
        self.run.side_effect = [1, 0]
        scheduler = self.scheduler()
        scheduler.tick()
        self.now += timedelta(minutes=5)
        scheduler.tick()
        self.assertEqual(scheduler.state["attempts"], 0)
        self.assertEqual(scheduler.state["last_exit_code"], 0)
        self.assertIn("last_success", scheduler.state)

    def test_interruption_leaves_slot_due(self):
        self.run.side_effect = InterruptedError
        with self.assertRaises(InterruptedError):
            self.scheduler().tick()
        self.run.side_effect = None
        self.assertTrue(self.scheduler().tick())

    def test_corrupt_state_is_not_silently_discarded(self):
        self.path.write_text("not-json")
        with self.assertRaisesRegex(UserError, "schedule.json"):
            self.scheduler()
        self.assertEqual(self.path.read_text(), "not-json")

    def test_schedule_change_runs_once_and_resets_anchor(self):
        self.scheduler().tick()
        self.schedule = Schedule.from_env({"SCHEDULE_MODE": "interval", "SCHEDULE_INTERVAL": "4h"})
        self.now += timedelta(hours=1)
        scheduler = self.scheduler()
        self.assertTrue(scheduler.tick())
        self.assertEqual(scheduler.state["next_run"], (self.now + timedelta(hours=4)).isoformat())

    def test_run_child_timeout_terminates_process(self):
        child = Mock()
        child.poll.return_value = None
        stopped = Mock()
        stopped.wait.return_value = False
        with patch("scheduler.subprocess.Popen", return_value=child), \
                patch("scheduler.clock_time.monotonic", side_effect=[0, 2]):
            self.assertEqual(run_child(Path("sync.py"), stopped, 1), 124)
        child.terminate.assert_called_once()
        child.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
