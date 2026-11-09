import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from strava.main import (ActivityDatabase, ActivityDownloader, CookieManager,
                         StravaAuth, StravaClient, StravaClientBuilder, StravaSettings,
                         TokenData)


class StravaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.database = ActivityDatabase(":memory:")
        self.addCleanup(self.database.close)

    def settings(self):
        return StravaSettings(_env_file=None, CLIENT_ID="id", CLIENT_SECRET="secret",
                              token_file=str(self.directory / "tokens.json"))

    def test_download_uses_cookie_session_not_oauth_session(self):
        with patch("strava.main.StravaSettings", return_value=self.settings()), \
                patch("strava.main.ActivityDatabase", return_value=self.database):
            builder = StravaClientBuilder()
            client = builder.build()
        self.assertIs(client.downloader.session, builder.cookie_manager.session)
        self.assertIsNot(client.downloader.session, builder.auth.session)

    def test_cookie_is_scoped_to_strava(self):
        target = self.directory / "cookie.json"
        target.write_text('{"_strava4_session": "fake"}')
        manager = CookieManager(str(target))
        manager.load_cookies()
        cookie = next(iter(manager.session.cookies))
        self.assertEqual(cookie.domain, ".strava.com")
        self.assertTrue(cookie.secure)

    def test_oauth_rejects_unrelated_callback_before_network_request(self):
        auth = StravaAuth(self.settings())
        with patch("strava.main.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "state"):
                auth._fetch_token("http://localhost/exchange_token?code=fake&state=wrong", "expected")
        post.assert_not_called()

    def test_oauth_callback_exchanges_code_with_timeout(self):
        auth = StravaAuth(self.settings())
        with patch("strava.main.requests.post") as post, patch.object(auth, "_save_tokens"):
            auth._fetch_token("http://localhost/exchange_token?code=fake&state=expected", "expected")
        self.assertEqual(post.call_args.kwargs["data"]["code"], "fake")
        self.assertEqual(post.call_args.kwargs["timeout"], 30)

    def test_download_401_requests_new_cookies_not_oauth_refresh(self):
        response = requests.Response()
        response.status_code = 401
        response.raw = Mock()
        session = Mock()
        session.get.return_value = response
        downloader = ActivityDownloader(session, self.database)
        with self.assertRaisesRegex(ValueError, "cookie.json"):
            downloader.download_activity(123)
        session.auth.refresh_token.assert_not_called()
        self.assertFalse(self.database.is_downloaded(123))

    def test_html_export_is_not_marked_downloaded(self):
        response = Mock(headers={"Content-Type": "text/html"})
        session = Mock()
        session.get.return_value = response
        downloader = ActivityDownloader(session, self.database)
        original_cwd = Path.cwd()
        try:
            os.chdir(self.directory)
            with self.assertRaisesRegex(ValueError, "web page"):
                downloader.download_activity(123)
        finally:
            os.chdir(original_cwd)
        self.assertFalse(self.database.is_downloaded(123))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_export_failure_does_not_leave_partial_file(self):
        response = Mock(headers={"Content-Type": "application/octet-stream"})
        def chunks(**_):
            yield b"some-fit-bytes"
            raise requests.Timeout
        response.iter_content.side_effect = chunks
        session = Mock()
        session.get.return_value = response
        downloader = ActivityDownloader(session, self.database)
        original_cwd = Path.cwd()
        try:
            os.chdir(self.directory)
            with self.assertRaises(requests.Timeout):
                downloader.download_activity(123)
        finally:
            os.chdir(original_cwd)
        self.assertFalse(self.database.is_downloaded(123))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_successful_export_is_marked_only_after_write(self):
        response = Mock(headers={"Content-Type": "application/octet-stream"})
        response.iter_content.return_value = [b"fit", b"bytes"]
        session = Mock()
        session.get.return_value = response
        downloader = ActivityDownloader(session, self.database)
        original_cwd = Path.cwd()
        try:
            os.chdir(self.directory)
            self.assertTrue(downloader.download_activity(123))
        finally:
            os.chdir(original_cwd)
        self.assertTrue(self.database.is_downloaded(123))
        self.assertEqual((self.directory / "activity_123_original.fit").read_bytes(), b"fitbytes")

    def test_api_paginates_and_matches_names_case_insensitively(self):
        auth = Mock()
        other = {"id": 1, "name": "Other", "type": "Ride", "start_date": "2026-01-01T12:00:00Z"}
        matching = {**other, "id": 2, "name": "mywhoosh ride", "type": "VirtualRide"}
        page1 = Mock(status_code=200)
        page1.json.return_value = [other] * 100
        page2 = Mock(status_code=200)
        page2.json.return_value = [matching]
        auth.session.get.side_effect = [page1, page2]
        activities = StravaClient(auth, Mock()).get_filtered_activities()
        self.assertEqual([a.id for a in activities], [2])
        self.assertEqual(auth.session.get.call_args.kwargs["params"]["page"], 2)

    def test_utc_token_expiry_is_supported(self):
        auth = StravaAuth(self.settings())
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        auth.token_data = TokenData(access_token="a", refresh_token="r", expires_at=expiry)
        self.assertTrue(auth._is_token_valid())

    def test_api_refresh_is_attempted_only_once(self):
        auth = Mock()
        response = requests.Response()
        response.status_code = 401
        auth.session.get.return_value = response
        client = StravaClient(auth, Mock())
        with self.assertRaises(requests.HTTPError):
            client.get_filtered_activities()
        auth.refresh_token.assert_called_once()
        self.assertEqual(auth.session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
