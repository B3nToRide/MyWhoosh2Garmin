# What does it do

**Optional / experimental.** This tool is not part of the MyWhoosh cloud sync,
the Docker image or the scheduler, and it does not upload anything to Garmin.

`main.py` lists activities through the Strava API and uses a separate website
cookie session to download original exports from
`https://www.strava.com/activities/{activity.id}/export_original`.
I did this because we are not allowed to scrape MyWhoosh site. It's in the TOS.

## Installation

### Register your app on Strava

1. Go to [Strava API settings](https://www.strava.com/settings/api)
2. In the `strava/` folder, create a private `.env` containing `CLIENT_ID` and
   `CLIENT_SECRET`. Do not commit or share these values.
3. Provide your own current Strava website cookies as a `cookie.json` object
   mapping cookie names to values. This is sensitive session data; protect it
   with `chmod 600 cookie.json`. Website cookies are independent of OAuth and
   must be replaced manually when expired. Do not grant them to untrusted code.
4. On first run, follow the printed OAuth authorisation URL and paste the callback
   URL into the terminal. The app's callback domain must permit `localhost`.

## Running

```
uv run main.py
```

Dependencies are declared at the top of `main.py` and pinned in `main.py.lock`, so `uv` resolves them on the first run.

Run from `strava/`: settings, token storage, cookies, SQLite state and downloaded
files currently use the working directory. OAuth tokens are written atomically
with private permissions; original downloads are marked in SQLite only after a
complete write. HTML login pages and empty responses are rejected. The current
downloader names exports `.fit`; exports in other original formats still need
manual handling. Validate an export before using it elsewhere.

Regression tests cover cookie-session wiring and failure handling, but no live
Strava account or website export has been validated by those tests. Strava can
change its website endpoints independently of the public API. Respect its terms
and your account's API limits.
