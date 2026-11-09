import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import myWhoosh2Garmin as sync
from runtime import LockBusy, UserError, atomic_target, process_lock


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_import_and_help_have_no_filesystem_side_effects(self):
        data = self.directory / "unused"
        env = {**os.environ, "MYWHOOSH_DATA_DIR": str(data)}
        script = str(Path(sync.__file__).resolve())
        subprocess.run([sys.executable, script, "--help"], env=env,
                       capture_output=True, check=True)
        subprocess.run([sys.executable, "-c", "import myWhoosh2Garmin"], env=env,
                       capture_output=True, check=True, cwd=Path(script).parent)
        self.assertFalse(data.exists())

    def test_missing_readings_are_not_zero_but_real_zero_is_kept(self):
        values = []
        for value in (150, None, 150, 0):
            sync.append_value(values, SimpleNamespace(heart_rate=value), "heart_rate")
        self.assertEqual(values, [150, 150, 0])
        self.assertEqual(sync.calculate_avg([150, None, 150]), 150)
        self.assertEqual(sync.calculate_avg(values), 100)
        self.assertEqual(sync.calculate_avg([]), 0)

    def test_export_selection_uses_mtime_not_version(self):
        old = self.directory / "MyNewActivity-6.2.0.fit"
        new = self.directory / "MyNewActivity-6.1.0.fit"
        gpx = self.directory / "MyNewActivity-6.2.0.gpx"
        for path, modified in ((old, 100), (new, 300), (gpx, 200)):
            path.touch()
            os.utime(path, (modified, modified))
        self.assertEqual(sync.get_most_recent_activity_file(self.directory), new)
        os.utime(gpx, (300, 300))
        self.assertEqual(sync.get_most_recent_activity_file(self.directory), new)
        os.utime(gpx, (400, 400))
        self.assertEqual(sync.get_most_recent_activity_file(self.directory), gpx)

    def test_windows_without_export_directory_fails_explicitly(self):
        with patch.object(sync.os, "name", "nt"), patch.object(sync.sys, "platform", "win32"), \
                patch.object(sync.Path, "home", return_value=self.directory):
            with self.assertRaisesRegex(UserError, "No valid MyWhoosh"):
                sync.get_fitfile_location()

    def test_explicit_local_directory(self):
        os.environ["MYWHOOSH_LOCAL_DIR"] = str(self.directory)
        self.assertEqual(sync.get_fitfile_location(), self.directory)

    def test_file_secrets_preserve_literal_password_and_override_env(self):
        password = "  p$ass'\\word  "
        secret = self.directory / "password"
        secret.write_text(password + "\n", encoding="utf-8")
        os.environ.update(MYWHOOSH_EMAIL="test@example.invalid", MYWHOOSH_PASSWORD="wrong",
                          MYWHOOSH_PASSWORD_FILE=str(secret))
        with patch.object(sync, "ENV_PATH", self.directory / "missing.env"):
            self.assertEqual(sync.get_credentials_for_mywhoosh(), ("test@example.invalid", password))

    def test_env_credentials_override_dotenv(self):
        envfile = self.directory / ".env"
        envfile.write_text("MYWHOOSH_EMAIL=file@example.invalid\nMYWHOOSH_PASSWORD='literal${VALUE}'\n")
        os.environ["MYWHOOSH_EMAIL"] = "env@example.invalid"
        with patch.object(sync, "ENV_PATH", envfile):
            self.assertEqual(sync.get_credentials_for_mywhoosh(),
                             ("env@example.invalid", "literal${VALUE}"))

    def test_atomic_failure_preserves_existing_backup(self):
        target = self.directory / "backup.fit"
        target.write_bytes(b"original")
        with self.assertRaises(ValueError):
            with atomic_target(target) as temporary:
                temporary.write_bytes(b"partial")
                raise ValueError("failed")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_mywhoosh_device_identity_is_generated_once_and_written_privately(self):
        with patch.object(sync, "DATA_DIR", self.directory):
            first = sync.get_mywhoosh_device_id()
            with patch.object(sync.uuid, "uuid4") as generate:
                second = sync.get_mywhoosh_device_id()
            generate.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(uuid.UUID(first).version, 4)
        target = self.directory / "mywhoosh_device_id.json"
        self.assertEqual(json.loads(target.read_text()), {"device_id": first})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_mywhoosh_device_identity_survives_process_restart(self):
        data = self.directory / "data"
        env = {**os.environ, "MYWHOOSH_DATA_DIR": str(data)}
        command = [sys.executable, "-c", "import myWhoosh2Garmin as s; print(s.get_mywhoosh_device_id())"]
        results = [subprocess.run(command, env=env, capture_output=True, text=True,
                                  check=True, cwd=Path(sync.__file__).parent).stdout.strip()
                   for _ in range(2)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(uuid.UUID(results[0]).version, 4)

    def test_invalid_device_identity_is_not_silently_replaced(self):
        target = self.directory / "mywhoosh_device_id.json"
        for content in ("not-json", "[]", "{}", '{"device_id":null}',
                        '{"device_id":"invalid"}', '{"device_id":123}'):
            with self.subTest(content=content):
                target.write_text(content)
                with patch.object(sync, "DATA_DIR", self.directory), \
                        patch.object(sync.requests, "post") as post:
                    with self.assertRaisesRegex(UserError, "Invalid mywhoosh_device_id.json"):
                        sync.authenticate_to_mywhoosh("email", "password")
                post.assert_not_called()
                self.assertEqual(target.read_text(), content)

    def test_login_reuses_device_id_but_changes_request_correlation(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"Success":true,"AccessToken":"fake-token","WhooshId":"fake-id"}'
        with patch.object(sync, "DATA_DIR", self.directory), \
                patch.object(sync.requests, "post", return_value=response) as post:
            for _ in range(2):
                self.assertEqual(sync.authenticate_to_mywhoosh("email", "password"),
                                 ("fake-token", "fake-id"))
        first, second = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual(first["DeviceId"], second["DeviceId"])
        self.assertNotEqual(first["CorrelationId"], second["CorrelationId"])
        self.assertEqual(first["Platform"], "Android")

    def test_device_identity_is_kept_after_refused_login(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"Success":false,"Message":"You are already logged in from another device."}'
        with patch.object(sync, "DATA_DIR", self.directory), \
                patch.object(sync.requests, "post", return_value=response) as post:
            for _ in range(2):
                with self.assertRaisesRegex(UserError, "another active session"):
                    sync.authenticate_to_mywhoosh("email", "password")
        identities = [call.kwargs["json"]["DeviceId"] for call in post.call_args_list]
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(json.loads((self.directory / "mywhoosh_device_id.json").read_text())
                         ["device_id"], identities[0])

    def test_device_identity_is_kept_after_network_timeout(self):
        with patch.object(sync, "DATA_DIR", self.directory), \
                patch.object(sync.requests, "post", side_effect=requests.Timeout) as post:
            for _ in range(2):
                with self.assertRaises(requests.Timeout):
                    sync.authenticate_to_mywhoosh("email", "password")
        self.assertEqual(post.call_args_list[0].kwargs["json"]["DeviceId"],
                         post.call_args_list[1].kwargs["json"]["DeviceId"])

    def test_lock_blocks_second_process_and_releases(self):
        lock = self.directory / "sync.lock"
        with process_lock(lock):
            with self.assertRaises(LockBusy):
                with process_lock(lock):
                    self.fail("Acquired a held lock")
        with process_lock(lock):
            pass

    def test_noninteractive_mode_never_prompts_without_tokens(self):
        with patch.object(sync, "TOKENS_PATH", self.directory / "missing"), \
                patch("builtins.input") as prompt:
            with self.assertRaisesRegex(UserError, "--login"):
                sync.authenticate_to_garmin(interactive=False)
            prompt.assert_not_called()

    def test_login_does_not_sync_mywhoosh(self):
        with patch.object(sync, "DATA_DIR", self.directory), \
                patch.object(sync, "configure_logging"), \
                patch.object(sync, "get_credentials_for_garmin") as login, \
                patch.object(sync, "sync_cloud_activities") as cloud:
            self.assertEqual(sync.main(["--login"]), 0)
            login.assert_called_once()
            cloud.assert_not_called()

    def test_simultaneous_watch_recording_does_not_mask_virtual_ride(self):
        self.assertFalse(sync.is_mywhoosh_candidate({"activityType": {"typeKey": "running"}}))
        self.assertFalse(sync.is_mywhoosh_candidate({"activityType": {"typeKey": "indoor_cycling"}}))
        self.assertTrue(sync.is_mywhoosh_candidate({"activityType": {"typeKey": "virtual_ride"}}))
        self.assertTrue(sync.is_mywhoosh_candidate({"activityType": {"typeKey": "cycling"},
                                                  "activityName": "MyWhoosh - Ride"}))

    def test_backup_names_are_utc_and_unambiguous_at_dst_transition(self):
        first = sync.mywhoosh_activity_filename({"startDatetime": "2026-10-25T02:30:00+02:00"})
        second = sync.mywhoosh_activity_filename({"startDatetime": "2026-10-25T02:30:00+01:00"})
        self.assertEqual(first, "MyWhoosh_2026-10-25_003000Z.fit")
        self.assertEqual(second, "MyWhoosh_2026-10-25_013000Z.fit")

    def cloud_mocks(self, activities, *, download_error=False):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, result in (("get_credentials_for_mywhoosh", ("email", "password")),
                             ("authenticate_to_mywhoosh", ("token", "id")),
                             ("get_recent_mywhoosh_activities", activities),
                             ("get_garmin_start_times", [])):
            stack.enter_context(patch.object(sync, name, return_value=result))
        stack.enter_context(patch.object(sync, "resolve_backup_target",
                                       side_effect=lambda filename: self.directory / filename))
        download = stack.enter_context(patch.object(sync, "download_mywhoosh_activity"))
        if download_error:
            download.side_effect = [requests.Timeout("secret-token-must-not-be-logged"), None]
        stack.enter_context(patch.object(sync, "cleanup_fit_file"))
        upload = stack.enter_context(patch.object(sync, "upload_fit_file_to_garmin", return_value=True))
        return download, upload

    def activities(self):
        now = datetime.now(timezone.utc)
        return [{"title": "Ride", "activityFileId": str(index),
                 "startDatetime": (now - timedelta(days=day)).isoformat()}
                for index, day in enumerate((2, 1))]

    def test_one_failed_download_does_not_block_later_upload(self):
        download, upload = self.cloud_mocks(self.activities(), download_error=True)
        with self.assertLogs(sync.logger, level="INFO") as logs:
            self.assertFalse(sync.sync_cloud_activities())
        self.assertEqual(download.call_count, 2)
        upload.assert_called_once()
        self.assertNotIn("secret-token-must-not-be-logged", "\n".join(logs.output))

    def test_one_failed_upload_does_not_block_later_upload(self):
        _, upload = self.cloud_mocks(self.activities())
        upload.side_effect = [False, True]
        self.assertFalse(sync.sync_cloud_activities())
        self.assertEqual(upload.call_count, 2)

    def test_malformed_metadata_does_not_block_valid_ride(self):
        _, upload = self.cloud_mocks([{"startDatetime": "invalid"}, self.activities()[0]])
        self.assertFalse(sync.sync_cloud_activities())
        upload.assert_called_once()

    def test_no_recent_activities_is_successful(self):
        self.cloud_mocks([])
        self.assertTrue(sync.sync_cloud_activities())

    def activity_response(self, activities):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"data": {"results": activities}}).encode()
        return response

    def test_mywhoosh_fetches_only_first_page_even_when_full_and_recent(self):
        now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        first = [{"activityFileId": str(i), "startDatetime": (now - timedelta(days=i)).isoformat()}
                 for i in range(10)]
        with patch.object(sync.requests, "post", return_value=self.activity_response(first)) as post:
            self.assertEqual(sync.get_recent_mywhoosh_activities("fake-token"), first)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"page": 1, "limit": 10, "sortDate": "DESC"})

    def test_mywhoosh_enforces_ten_activity_limit_if_api_returns_more(self):
        now = datetime.now(timezone.utc)
        activities = [{"activityFileId": str(i), "startDatetime": now.isoformat()} for i in range(15)]
        with patch.object(sync.requests, "post", return_value=self.activity_response(activities)) as post:
            result = sync.get_recent_mywhoosh_activities("fake-token")
        self.assertEqual(result, activities[:10])
        post.assert_called_once()

    def test_mywhoosh_empty_history_needs_only_one_request(self):
        with patch.object(sync.requests, "post", return_value=self.activity_response([])) as post:
            self.assertEqual(sync.get_recent_mywhoosh_activities("fake-token"), [])
        post.assert_called_once()

    def test_mywhoosh_invalid_activity_list_is_rejected(self):
        for activities in (None, {}, [None]):
            with self.subTest(activities=activities), \
                    patch.object(sync.requests, "post", return_value=self.activity_response(activities)):
                with self.assertRaisesRegex(UserError, "invalid activity page"):
                    sync.get_recent_mywhoosh_activities("fake-token")

    def test_sync_includes_october_23_and_21_day_boundary_but_not_older(self):
        self.assertEqual(sync.CATCH_UP_WINDOW, timedelta(days=21))
        activities = [
            {"activityFileId": "oct23", "startDatetime": "2026-10-23T12:00:00Z"},
            {"activityFileId": "boundary", "startDatetime": "2026-10-19T12:00:00Z"},
            {"activityFileId": "old", "startDatetime": "2026-10-19T11:59:59Z"},
        ]
        download, upload = self.cloud_mocks(activities)
        with patch.object(sync, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
            self.assertTrue(sync.sync_cloud_activities())
        self.assertEqual(upload.call_count, 2)
        self.assertEqual([call.args[2]["activityFileId"] for call in download.call_args_list],
                         ["boundary", "oct23"])

    def test_already_uploaded_ride_is_not_downloaded(self):
        activities = self.activities()
        download, upload = self.cloud_mocks(activities)
        with patch.object(sync, "get_garmin_start_times",
                          return_value=[sync.mywhoosh_start_time(a) for a in activities]):
            self.assertTrue(sync.sync_cloud_activities())
        download.assert_not_called()
        upload.assert_not_called()

    def test_http_200_import_failure_is_not_success(self):
        target = self.directory / "ride.fit"
        target.touch()
        with patch.object(sync.garth.client, "upload", return_value={
                "detailedImportResult": {"failures": [{"messages": ["invalid"]}]}}):
            self.assertFalse(sync.upload_fit_file_to_garmin(target))

    def test_http_200_without_import_confirmation_is_not_success(self):
        target = self.directory / "ride.fit"
        target.touch()
        with patch.object(sync.garth.client, "upload", return_value={}):
            self.assertFalse(sync.upload_fit_file_to_garmin(target))

    def test_confirmed_import_is_success(self):
        target = self.directory / "ride.fit"
        target.touch()
        with patch.object(sync.garth.client, "upload", return_value={
                "detailedImportResult": {"successes": [{"internalId": 1}], "failures": []}}):
            self.assertTrue(sync.upload_fit_file_to_garmin(target))

    def test_documented_garth_acknowledgement_with_empty_results_is_accepted(self):
        target = self.directory / "ride.fit"
        target.touch()
        # Shape documented at garth.readthedocs.io/en/latest/api/connect-api/.
        response = {"detailedImportResult": {"uploadId": 12345,
                    "uploadUuid": {"uuid": "fake-uuid"}, "owner": "private-owner",
                    "successes": [], "failures": []}}
        with patch.object(sync.garth.client, "upload", return_value=response), \
                self.assertLogs(sync.logger, level="INFO") as logs:
            self.assertTrue(sync.upload_fit_file_to_garmin(target))
        output = "\n".join(logs.output)
        self.assertIn("Garmin accepted", output)
        self.assertIn("activity processing may continue", output)
        self.assertNotIn("Uploaded", output)
        self.assertNotIn("private-owner", output)
        self.assertNotIn("fake-uuid", output)

    def test_failures_override_upload_acknowledgement_and_successes(self):
        target = self.directory / "ride.fit"
        target.touch()
        response = {"detailedImportResult": {"uploadId": 12345,
                    "successes": [{"internalId": 1}],
                    "failures": [{"messages": [{"code": 999,
                                  "content": "sensitive signed URL and token"}]}]}}
        with patch.object(sync.garth.client, "upload", return_value=response), \
                self.assertLogs(sync.logger, level="ERROR") as logs:
            self.assertFalse(sync.upload_fit_file_to_garmin(target))
        output = "\n".join(logs.output)
        self.assertIn("codes: 999", output)
        self.assertNotIn("sensitive signed URL and token", output)

    def test_empty_result_without_valid_upload_id_is_not_accepted(self):
        target = self.directory / "ride.fit"
        target.touch()
        for upload_id in (None, 0, -1, True, "not-an-id"):
            response = {"detailedImportResult": {"uploadId": upload_id,
                        "successes": [], "failures": []}}
            with self.subTest(upload_id=upload_id), \
                    patch.object(sync.garth.client, "upload", return_value=response):
                self.assertFalse(sync.upload_fit_file_to_garmin(target))

    def test_malformed_upload_response_is_not_accepted(self):
        target = self.directory / "ride.fit"
        target.touch()
        responses = (None, [], {"detailedImportResult": []},
                     {"detailedImportResult": {"uploadId": 1, "failures": "invalid"}})
        for response in responses:
            with self.subTest(response=response), \
                    patch.object(sync.garth.client, "upload", return_value=response):
                self.assertFalse(sync.upload_fit_file_to_garmin(target))

    def test_http_409_duplicate_is_success(self):
        response = requests.Response()
        response.status_code = 409
        error = sync.GarthHTTPError(msg="upload failed", error=requests.HTTPError(response=response))
        target = self.directory / "ride.fit"
        target.touch()
        with patch.object(sync.garth.client, "upload", side_effect=error):
            self.assertTrue(sync.upload_fit_file_to_garmin(target))

    def test_session_tokens_are_written_privately(self):
        tokens = self.directory / ".garth"
        def save(directory):
            (Path(directory) / "oauth1_token.json").write_text('{"token":"fake"}')
        with patch.object(sync, "DATA_DIR", self.directory), \
                patch.object(sync, "TOKENS_PATH", tokens), \
                patch.object(sync.garth, "save", side_effect=save):
            sync.save_garmin_session()
        self.assertEqual(tokens.stat().st_mode & 0o777, 0o700)
        self.assertEqual((tokens / "oauth1_token.json").stat().st_mode & 0o777, 0o600)

    def test_garmin_pagination_filters_unrelated_activities(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        virtual = {"startTimeGMT": "2026-01-01T10:00:00", "activityType": {"typeKey": "virtual_ride"}}
        running = {**virtual, "activityType": {"typeKey": "running"}}
        with patch.object(sync.garth, "connectapi", side_effect=[[virtual] * 100, [running]]) as api:
            result = sync.get_garmin_start_times([start])
        self.assertEqual(len(result), 100)
        self.assertEqual(api.call_args_list[1].kwargs["params"]["start"], 100)


class FitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def make_gpx(self):
        source = self.directory / "source.gpx"
        source.write_text('''<gpx xmlns="http://www.topografix.com/GPX/1/1"
          xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
          <trk><trkseg>
            <trkpt lat="48.0" lon="2.0"><time>2026-01-01T10:00:00Z</time>
              <extensions><power>100</power><gpxtpx:TrackPointExtension>
                <gpxtpx:hr>150</gpxtpx:hr><gpxtpx:cad>80</gpxtpx:cad>
              </gpxtpx:TrackPointExtension></extensions></trkpt>
            <trkpt lat="48.00001" lon="2.0"><time>2026-01-01T10:00:01Z</time></trkpt>
            <trkpt lat="48.00002" lon="2.0"><time>2026-01-01T10:00:02Z</time>
              <extensions><power>0</power><gpxtpx:TrackPointExtension>
                <gpxtpx:hr>150</gpxtpx:hr><gpxtpx:cad>0</gpxtpx:cad>
              </gpxtpx:TrackPointExtension></extensions></trkpt>
          </trkseg></trk></gpx>''')
        return source

    def test_gpx_to_fit_round_trip_preserves_missing_measurements(self):
        target = self.directory / "ride.fit"
        sync.convert_gpx_to_fit(self.make_gpx(), target)
        messages = [record.message for record in sync.FitFile.from_file(str(target)).records]
        records = [m for m in messages if isinstance(m, sync.RecordMessage)]
        session = next(m for m in messages if isinstance(m, sync.SessionMessage))
        lap = next(m for m in messages if isinstance(m, sync.LapMessage))
        self.assertEqual(len(records), 3)
        self.assertIsNone(records[1].heart_rate)
        self.assertIsNone(records[1].power)
        self.assertEqual(records[2].power, 0)
        for summary in (session, lap):
            self.assertEqual(summary.avg_power, 50)
            self.assertEqual(summary.avg_heart_rate, 150)
            self.assertEqual(summary.avg_cadence, 40)
        self.assertAlmostEqual(session.total_elapsed_time, 2)
        self.assertGreater(session.total_distance, 0)
        file_id = next(m for m in messages if isinstance(m, sync.FileIdMessage))
        device = next(m for m in messages if isinstance(m, sync.DeviceInfoMessage))
        self.assertEqual(file_id.manufacturer, 1)
        self.assertEqual(file_id.product, 3843)
        self.assertEqual(device.device_index, 0)
        self.assertEqual(device.product, 3843)
        self.assertEqual(device.product_name, "Edge 1040")
        self.assertIsNone(device.serial_number)

    def test_cleanup_fills_missing_averages_and_removes_temperature(self):
        source = self.directory / "input.fit"
        builder = sync.FitFileBuilder(auto_define=True)
        file_id = sync.FileIdMessage()
        file_id.type = sync.FileType.ACTIVITY
        builder.add(file_id)
        for index, value in enumerate((150, None, 150)):
            record = sync.RecordMessage()
            record.timestamp = 1767261600000 + index * 1000
            record.temperature = 20
            if value is not None:
                record.heart_rate = value
            record.power = 0
            builder.add(record)
        session = sync.SessionMessage()
        session.timestamp = 1767261602000
        builder.add(session)
        builder.build().to_file(str(source))
        target = self.directory / "fixed.fit"
        sync.cleanup_fit_file(source, target)
        messages = [r.message for r in sync.FitFile.from_file(str(target)).records]
        fixed = next(m for m in messages if isinstance(m, sync.SessionMessage))
        self.assertEqual(fixed.avg_heart_rate, 150)
        self.assertEqual(fixed.avg_power, 0)
        self.assertTrue(all(m.temperature is None for m in messages if isinstance(m, sync.RecordMessage)))
        file_id = next(m for m in messages if isinstance(m, sync.FileIdMessage))
        device = next(m for m in messages if isinstance(m, sync.DeviceInfoMessage))
        self.assertEqual(file_id.product, 3843)
        self.assertEqual(device.device_index, 0)
        self.assertEqual(device.product_name, "Edge 1040")

    def test_cleanup_updates_primary_device_but_preserves_sensor_metadata(self):
        builder = sync.FitFileBuilder()
        file_id = sync.FileIdMessage()
        file_id.type = sync.FileType.ACTIVITY
        builder.add(file_id)
        for index, name in ((0, "Old recording device"), (1, "Trainer")):
            device = sync.DeviceInfoMessage()
            device.device_index = index
            device.manufacturer = 32
            device.product = 99
            device.product_name = name
            device.serial_number = 12345 + index
            builder.add(device)
        source = self.directory / "input.fit"
        builder.build().to_file(str(source))
        target = self.directory / "fixed.fit"
        sync.cleanup_fit_file(source, target)
        devices = [r.message for r in sync.FitFile.from_file(str(target)).records
                   if isinstance(r.message, sync.DeviceInfoMessage)]
        self.assertEqual(len(devices), 2)
        primary, sensor = devices
        self.assertEqual((primary.manufacturer, primary.product, primary.product_name),
                         (1, 3843, "Edge 1040"))
        self.assertEqual(primary.serial_number, 12345)
        self.assertEqual((sensor.device_index, sensor.manufacturer, sensor.product,
                          sensor.product_name, sensor.serial_number),
                         (1, 32, 99, "Trainer", 12346))

    def test_invalid_gpx_does_not_overwrite_existing_fit(self):
        source = self.make_gpx()
        source.write_text(source.read_text().replace("10:00:02Z", "09:00:00Z"))
        target = self.directory / "existing.fit"
        target.write_bytes(b"original")
        with self.assertRaises(ValueError):
            sync.convert_gpx_to_fit(source, target)
        self.assertEqual(target.read_bytes(), b"original")

    def test_cleanup_preserves_existing_averages_including_zero(self):
        builder = sync.FitFileBuilder()
        record = sync.RecordMessage()
        record.power = 100
        record.heart_rate = 150
        builder.add(record)
        session = sync.SessionMessage()
        session.avg_power = 0
        session.avg_heart_rate = 120
        builder.add(session)
        source = self.directory / "input.fit"
        builder.build().to_file(str(source))
        target = self.directory / "fixed.fit"
        sync.cleanup_fit_file(source, target)
        fixed = next(r.message for r in sync.FitFile.from_file(str(target)).records
                     if isinstance(r.message, sync.SessionMessage))
        self.assertEqual(fixed.avg_power, 0)
        self.assertEqual(fixed.avg_heart_rate, 120)


if __name__ == "__main__":
    unittest.main()
