# myWhoosh2Garmin

This fork extends [jcanton/MyWhoosh2Garmin](https://github.com/jcanton/MyWhoosh2Garmin)
with Docker automation and sync reliability improvements. Original credits and
license are retained.

## Choose how to run it

| Mode | Use it for | Requirements |
|---|---|---|
| [Docker Compose](docs/docker.md) — recommended | Scheduled or one-shot cloud sync on a Mac mini | Docker Desktop / Compose v2; no host Python or PowerShell |
| [Native CLI](#native-installation) | Manual cloud sync on macOS, Windows or Linux | `uv` and the complete repository |
| [Android / Termux](#android--termux) | Manual sync from a home-screen shortcut | Termux and Termux:Widget; setup uses Termux Python, not `uv` |
| [Local exports](#local-exports) | Import the newest FIT/GPX from the MyWhoosh app | Native CLI and access to the export folder |

Cloud sync does not require the MyWhoosh app to be installed on the machine
running the script. Strava and the [legacy launchers](#legacy-launchers) are
optional and are not prerequisites for any of the cloud-sync modes above.

## Automatic sync with Docker

Run on a Mac mini (Apple Silicon or Intel) with Docker Compose. The container
supports **daily sync at 20:00 Europe/Paris** by default, or an elapsed-time
interval such as **every 4 hours**. Missed executions are caught up once after
restart. A one-shot sync remains available.

**[Installation, secrets, scheduling and troubleshooting — Docker guide (français)](docs/docker.md)**

After configuring `.docker.env`, private secret files and the `data/` folder as
described in the guide:

```sh
docker compose --env-file .docker.env build --pull
docker compose --env-file .docker.env run --rm sync --login
docker compose --env-file .docker.env up -d
```

No credentials belong in the image or Compose file. MyWhoosh credentials are
read from mounted files; Garmin is authenticated interactively once and its
session is persisted. Compose file secrets are **not encrypted storage**.

## Features

*   Downloads your rides from the MyWhoosh cloud, the same .fit files the MyWhoosh website offers under ACTIVITY FILES. It works whichever device you rode on.
*   Catches up: of your MyWhoosh rides from the last **21 days** (up to the **10 most recent**), it uploads every one that Garmin Connect does not have yet. A matching virtual ride, or a cycling activity named MyWhoosh, within 60 seconds of the start counts as already present. This is a heuristic, not an exact activity-ID comparison.
*   With `--local`, processes the most recently modified FIT or GPX export from the MyWhoosh app folder. GPX is converted to FIT; an older FIT does not take precedence over a newer GPX.
*   Fills missing power, cadence and heart-rate averages without treating absent sensor readings as zero. Existing averages, including real zero values, are preserved. Calculated averages are sample-based, not time-weighted.
*   Removes the temperature.
*   Sets the FIT recording-device metadata to **Garmin Edge 1040** (manufacturer 1, product 3843), leaving attached sensors unchanged. This applies to new imports, not activities already on Garmin, and does not guarantee device-specific training metrics.
*   Saves backups atomically in `backups/`, or a configured folder.
*   Uploads the fixed .fit file to Garmin Connect.
*   Processes each ride independently: one failed download or upload does not block the remaining rides.
*   Prevents overlapping syncs sharing the same data directory, and logs progress to the console.
*   Reuses a persistent MyWhoosh device UUID (`mywhoosh_device_id.json` in the data directory) across retries and container restarts, instead of presenting a different device on every login. This does not guarantee that existing server-side session conflicts are resolved.

## Native installation

These instructions are for the native CLI, **not Docker or Android**.

### 1. Install uv and clone this fork

Install `uv` using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/)
for your operating system, then clone the complete repository:

```sh
git clone https://github.com/B3nToRide/MyWhoosh2Garmin.git
cd MyWhoosh2Garmin
```

Keep `runtime.py` and `scheduler.py` next to `myWhoosh2Garmin.py`; downloading the
main Python file alone is no longer sufficient. Run the remaining commands from
the repository folder, in Terminal on macOS/Linux or a terminal on Windows.

`uv` selects the Python **3.13** version declared in the script and installs its
locked dependencies in a cached environment. No host virtual environment needs
to be created or activated. Python, `garth`, `fit_tool`, `requests` and
`python-dotenv` are handled by `uv`; Tkinter is not required.

### 2. Configure MyWhoosh credentials for cloud sync

Create an empty `.env` next to the main script and restrict access **before**
entering credentials. On macOS/Linux:

```sh
touch .env
chmod 600 .env
```

On Windows use the file security settings to restrict access to your account.
Then edit the file in a text editor:

```dotenv
MYWHOOSH_EMAIL=you@example.com
MYWHOOSH_PASSWORD='your-mywhoosh-password'
```

The values above are placeholders, not commands to paste your real password into.
The file is ignored by Git but **not encrypted**. Never add Garmin credentials
to this file.

Environment variables override `.env` values with the same name. Alternatively,
`MYWHOOSH_EMAIL_FILE` and `MYWHOOSH_PASSWORD_FILE` can point to private UTF-8
files; each `_FILE` setting takes precedence over the corresponding direct
value. File secrets are useful for passwords with complex quoting. The original
password is required by MyWhoosh; storing its SHA-256 cannot replace it.

These MyWhoosh credentials are unnecessary when using only `--local`.

### 3. Authenticate Garmin in an interactive terminal

```sh
uv run --locked myWhoosh2Garmin.py --login
```

Enter your Garmin username/password and MFA code if requested. This command
authenticates only: it does not sync activities. The password is not saved by
the script; the session is stored in `.garth/` under the data directory. Protect
these tokens like a password. Run this command again if the session expires.

`--locked` uses `myWhoosh2Garmin.py.lock` and fails instead of silently changing
the dependency resolution if the lock is stale. To install dependencies without
authenticating or syncing, use `uv sync --script myWhoosh2Garmin.py --locked`.

### 4. Run a one-shot cloud sync

Close any open MyWhoosh apps, then run:

```sh
uv run --locked myWhoosh2Garmin.py --once
```

Only the **10 most recent MyWhoosh activities**, at most **21 days old**, are
considered. Recognised Garmin activities are skipped; a failure on one ride
does not prevent processing the others. This is a real upload, not a dry run.

For unattended execution, authenticate once as above and use:

```sh
uv run --locked myWhoosh2Garmin.py --once --non-interactive
```

This fails instead of prompting if Garmin needs authentication. MyWhoosh can
refuse a login because another session is active; it does not identify which
device is involved. A previous session can also be involved, so this error does
not prove that an app is still open. The stable device UUID is preserved, but
MyWhoosh access tokens are not cached between runs.

Backups default to `backups/` under the data directory. There is **no folder
selection dialog**. Set `MYWHOOSH_BACKUP_DIR` to choose another folder; a valid
legacy `backup_path.json` in the data directory is still supported.

## Local exports

```sh
uv run --locked myWhoosh2Garmin.py --local
```

This imports only the newest `MyNewActivity-*.fit` or `MyNewActivity-*.gpx` by
modification time, not by the version number in its name. FIT wins a timestamp
tie. GPX is converted to FIT. There is no cloud-history catch-up in this mode;
running it again can submit the same export, subject to Garmin duplicate checks.

The script looks for the known macOS app-container folder or Windows package
folder. If that layout does not match your installation, set the actual
environment variable `MYWHOOSH_LOCAL_DIR` to your export directory. It is
required for `--local` on Linux. Local export-folder detection is not used by
cloud sync or the default Docker service.

## Legacy launchers

**Use the Docker scheduler for the documented automatic workflow.** The old
launchers are retained for reference, not as verified installation paths:

- [`MyWhooshMonitor.ps1`](MyWhooshMonitor.ps1) targets macOS and contains an
  original-author-specific Python script path. It is not ready to run unchanged
  and is not a Windows launcher. The old untested Windows snippet is no longer
  presented as a supported setup. PowerShell is not needed for Docker or the
  native macOS CLI.
- The optional [AppleScript guide](apple-script/README.md) explains the required
  adaptations and non-interactive login limitations. It monitors MyWhoosh
  quitting; it is not a daily/interval scheduler and has not been live-tested
  as part of this fork.

Neither launcher is used by Docker. Their executable files are unchanged.

## Android / Termux

The script runs in <a href="https://termux.dev">Termux</a>, started from a home-screen icon that opens a window with the log. Because rides come from the MyWhoosh cloud, it works whichever device you rode on.

<p>1. Install <b>Termux</b> and <b>Termux:Widget</b> from <a href="https://f-droid.org/packages/com.termux/">F-Droid</a>. Take both from the same source: Termux plugins only work with a Termux installed from the same place.</p>

<p>2. In Termux, clone the repository and run the setup:</p>

```
pkg install git
git clone https://github.com/B3nToRide/MyWhoosh2Garmin.git
cd MyWhoosh2Garmin
bash android/setup.sh
```

The setup installs Termux Python and Rust, creates or reuses `.venv`, installs
the pinned dependencies, and creates the shortcut. It asks for MyWhoosh
credentials only when `.env` is absent or empty; it does not repair an incomplete
non-empty `.env`. Keep that file private (`chmod 600 .env`).

Rust is available to build `pydantic-core` **if no compatible wheel is available**.
Compilation may take several minutes depending on the device; the previous
10–15 minute estimate is not a guarantee. This path uses Termux Python rather
than the Python version managed by `uv` on desktop.

Setup then launches a **real first sync**, including a Garmin prompt when the
terminal is interactive. To authenticate separately or repair the session,
run in Termux:

```sh
.venv/bin/python myWhoosh2Garmin.py --login
```

<p>3. Put the icon on the home screen. In Termux, run:</p>

```
am start -n com.termux.widget/.TermuxCreateShortcutActivity
```

Tap <b>MyWhoosh2Garmin</b> in the list and confirm the pin request. The icon comes
from <code>android/icon.png</code>. Availability of shortcut pinning, the list
widget and long-press menus depends on the launcher and Termux:Widget version;
use the Termux:Widget UI if the command is unavailable. After changing the icon,
remove and recreate the shortcut.

<p>4. After a ride, close MyWhoosh (swipe it away from recent apps) and tap the icon. The window shows the log and stays open until you press a key.</p>

This is a **manual-launch shortcut**, not a reliable Android background
scheduler. The Termux/Widget flow is retained from upstream; it has not been
live-tested on an Android device as part of this fork.

To update, run `git pull --ff-only` in the checkout, then `bash android/setup.sh`
again. It keeps a non-empty `.env` and reuses `.venv`. A Termux Python upgrade
can require recreating the virtual environment; credentials and `.garth/` are
stored separately, so back them up privately before maintenance.

The Android setup uses `pip`, not the desktop `uv` workflow.
`android/requirements.txt` exports the same pinned versions as
`myWhoosh2Garmin.py.lock`, with hashes; Docker shares this dependency list. After
changing the dependencies, regenerate it on a machine with `uv`:

```
uv export --script myWhoosh2Garmin.py --no-header -o android/requirements.txt
```

## CLI and runtime configuration

```sh
uv run --locked myWhoosh2Garmin.py --once
uv run --locked myWhoosh2Garmin.py --once --non-interactive
uv run --locked myWhoosh2Garmin.py --local
uv run --locked myWhoosh2Garmin.py --schedule
```

`--once` is the default. `--local` imports only the newest local export, without
cloud catch-up. On Linux or for a custom export location, set
`MYWHOOSH_LOCAL_DIR`. For scheduling options, see [the Docker guide](docs/docker.md);
the same environment variables work outside Docker.

For native scheduling, first complete the interactive `--login` step. The
scheduler launches non-interactive child syncs and never prompts for credentials.
Keep its data directory unchanged across runs to preserve its state.

`MYWHOOSH_DATA_DIR` moves runtime files (session, locks, schedule state and default
backups) out of the checkout. `MYWHOOSH_BACKUP_DIR` overrides the backup location.
`MYWHOOSH_EMAIL_FILE` and `MYWHOOSH_PASSWORD_FILE` read credentials from files and
take precedence over direct values. Only credential settings are loaded from
the script's `.env`; other runtime settings must be actual environment variables.

By default the data directory is the script folder; it must be writable.
Changing `MYWHOOSH_DATA_DIR` changes where Garmin sessions, the MyWhoosh device
UUID and scheduling state are read, not where `.env` is read. Restore these
files into the new directory or authenticate again as appropriate.

One-shot exit codes: **0** success/nothing new; **1** failed sync or invalid configuration;
**2** invalid CLI arguments; **75** another instance holds the lock. Native runs
also keep a rotating log (5 MiB × 4 files including the current file). Set
`LOG_TO_FILE=0` for console-only logging, as in Docker.

## Reading the logs

- `Already on Garmin Connect` means a matching activity was found, not that it
  was uploaded today or by this script.
- `Uploaded ...` means Garmin returned a successful import result.
- `Garmin accepted ... activity processing may continue` means Garmin
  acknowledged the file without a completed result list; this is not an error,
  nor proof that the activity is already visible in Connect.
- `Sync complete: ... submitted, ... already present, ... failed` counts
  accepted/confirmed submissions, skips and errors.

Cloud backup names use the ride start time in UTC (`Z` suffix), **not the date
of import**. Native rotating logs can retain recent execution history, but are
not a permanent per-activity audit trail. Docker logs belong to their container;
recreating/removing it, or using `run --rm`, can remove that history. Persistent
`schedule.json` records scheduler attempts, not the import time of every ride.

## Tests

No real credentials or service connections are used:

```sh
uv run --python 3.13 --with-requirements android/requirements.txt --with-requirements tests/requirements.txt python -m unittest discover -s tests -v
```

The optional [Strava tool](strava/README.md) is separate and is not included in
the Docker sync pipeline.

## Credits and license

- [jcanton/MyWhoosh2Garmin](https://github.com/jcanton/MyWhoosh2Garmin): upstream project.
- [Garth](https://github.com/matin/garth): Garmin authentication and upload.
- [mywhoosh-to-garmin](https://github.com/marcelorodrigo/mywhoosh-to-garmin): MyWhoosh cloud API endpoint references.
- [fit_tool](https://bitbucket.org/stagescycling/fit_tool/src/main/): FIT parsing and writing.
- [mw2gc](https://github.com/embeddedc/mw2gc): original average-correction example.

The original [GPLv3 license](LICENSE) is retained.
