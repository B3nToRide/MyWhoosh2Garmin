<h1 align="center" id="title">myWhoosh2Garmin</h1>

This fork extends [jcanton/MyWhoosh2Garmin](https://github.com/jcanton/MyWhoosh2Garmin)
with Docker automation and sync reliability improvements. Original credits and
license are retained.

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

<h2>🧐Features</h2>

*   Downloads your rides from the MyWhoosh cloud, the same .fit files the MyWhoosh website offers under ACTIVITY FILES. It works whichever device you rode on.
*   Catches up: of your MyWhoosh rides from the last **21 days** (up to the **10 most recent**), it uploads every one that Garmin Connect does not have yet. A matching virtual ride, or a cycling activity named MyWhoosh, within 60 seconds of the start counts as already present. This is a heuristic, not an exact activity-ID comparison.
*   With `--local`, reads the most recent export from the MyWhoosh app folder instead, .fit or .gpx. A .gpx export is converted into a .fit file when it is the newer of the two: MyWhoosh 6.2.0 wrote MyNewActivity-&lt;version&gt;.gpx for one ride and MyNewActivity-&lt;version&gt;.fit for the next, so both can sit in the folder at once and the older one must not win.
*   Fills missing power, cadence and heart-rate averages without treating absent sensor readings as zero. Existing averages, including real zero values, are preserved. Calculated averages are sample-based, not time-weighted.
*   Removes the temperature.
*   Sets the FIT recording-device metadata to **Garmin Edge 1040** (manufacturer 1, product 3843), leaving attached sensors unchanged. This applies to new imports, not activities already on Garmin, and does not guarantee device-specific training metrics.
*   Saves backups atomically in `backups/`, or a configured folder.
*   Uploads the fixed .fit file to Garmin Connect.
*   Processes each ride independently: one failed download or upload does not block the remaining rides.
*   Prevents overlapping syncs sharing the same data directory, and logs progress to the console.
*   Reuses a persistent MyWhoosh device UUID (`mywhoosh_device_id.json` in the data directory) across retries and container restarts, instead of registering a different device on every login. This does not guarantee that existing server-side session conflicts are resolved.

<h2>🛠️ Installation Steps:</h2>

<p>1. Install <code>uv</code> (if not already installed):</p>

- <b>MacOS / Linux:</b>

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

- <b>Windows</b> (PowerShell):

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

<p>2. Clone the repository. Keep <code>runtime.py</code> and <code>scheduler.py</code> alongside the main script:</p>

```sh
git clone https://github.com/B3nToRide/MyWhoosh2Garmin.git
cd MyWhoosh2Garmin
```

<p>3. Run the following commands from the repository folder.</p>

- <b>MacOS:</b> Terminal of your choice. 
- <b>Windows:</b> Start > Run > cmd or Start > Run > powershell

<p>4. Put your MyWhoosh credentials in a file named <code>.env</code> next to the script:</p>

```
MYWHOOSH_EMAIL=you@example.com
MYWHOOSH_PASSWORD=your-mywhoosh-password
```

`.env` is in `.gitignore`, so it stays out of the repository. Keep it private, for example with `chmod 600 .env`. Environment variables of the same name take precedence over the file. Only the MyWhoosh password goes here: Garmin Connect is authenticated once interactively, and its session is kept in `.garth/`.

<p>5. Run the script:</p>

```
uv run myWhoosh2Garmin.py
```

The dependencies and the required Python version are declared at the top of the script itself, so `uv` fetches Python 3.13 and the dependencies (`garth`, `fit_tool`, `requests`, `python-dotenv`) into a cached environment on the first run. There is no virtual environment to create or activate.

Exact versions are pinned in `myWhoosh2Garmin.py.lock`, which `uv run` picks up automatically. Two optional extras:

```
uv sync --script myWhoosh2Garmin.py   # install everything up front instead of on first run
uv run --locked myWhoosh2Garmin.py    # fail instead of re-resolving if the lock is stale
```
  
<p>6. Backups use <code>backups/</code> next to the script by default. Set the environment variable <code>MYWHOOSH_BACKUP_DIR</code> to override it. Existing <code>backup_path.json</code> configurations are still supported; there is no folder-selection dialog.</p>

<p>7. Enter your Garmin Connect credentials</p>

You can authenticate separately with `uv run --locked myWhoosh2Garmin.py --login`
(including an MFA prompt when required). Unattended runs use
`--once --non-interactive` and fail clearly instead of prompting when the session
is missing or expired.

```
2024-11-21 10:08:04,014 No existing session. Please log in.
Username: <YOUR_EMAIL>
Password:
2024-11-21 10:08:33,545 Authenticating...

2024-11-21 10:08:37,107 Successfully authenticated!
```

<p>8. Run the script when you're done riding, after quitting MyWhoosh.</p>

MyWhoosh can refuse login when another session is still active. This does not identify the device: an open app or a previous session may be involved. Close any open MyWhoosh apps before retrying:

```
MyWhoosh reports another active session; close any open MyWhoosh apps and retry later. A previous session may still be active
```

A successful run looks like this:

```
2026-09-23 09:04:40,455 Authenticated to MyWhoosh.
2026-09-23 09:04:46,378 Already on Garmin Connect: MyWhoosh - Base (2026-09-20T13:12:50.000Z).
2026-09-23 09:04:46,378 New MyWhoosh activity: MyWhoosh - Arctic Trail (2026-09-23T06:32:48.000Z).
2026-09-23 09:04:46,920 Cleaned-up file saved as <YOUR_BACKUP_FOLDER>/MyWhoosh_2026-09-23_063248Z.fit
```

Each cloud backup is named after the ride's UTC start time (`Z` suffix), so a change of host timezone or daylight-saving time cannot change its name. Running the script again uploads nothing new when its rides are already recognised on Garmin Connect.

To use the app folder instead of the cloud (newest export only, no catch-up):

```
uv run myWhoosh2Garmin.py --local
```

<p>(9. Or see below to automate the process)</p>

<h2>ℹ️ Automation tips</h2> 

**Docker Compose is the recommended automation method; see [the guide](docs/docker.md).**
The PowerShell and AppleScript examples below are legacy launchers, not part of
the Docker workflow. PowerShell is not maintained by this change.

What if you want to automate the whole process:
<h3>MacOS</h3>

PowerShell on MacOS (Verified & works)

You need Powershell

```shell
brew install powershell/tap/powershell
```

```powershell
# Define the JSON config file path
$configFile = "$PSScriptRoot\mywhoosh_config.json"
$myWhooshApp = "myWhoosh Indoor Cycling App.app"

# Check if the JSON file exists and read the stored path
if (Test-Path $configFile) {
    $config = Get-Content -Path $configFile | ConvertFrom-Json
    $mywhooshPath = $config.path
} else {
    $mywhooshPath = $null
}

# Validate the stored path
if (-not $mywhooshPath -or -not (Test-Path $mywhooshPath)) {
    Write-Host "Searching for $myWhooshApp"
    $mywhooshPath = Get-ChildItem -Path "/Applications" -Filter $myWhooshApp -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1

    if (-not $mywhooshPath) {
        Write-Host " not found!"
        exit 1
    }

    $mywhooshPath = $mywhooshPath.FullName

    # Store the path in the JSON file
    $config = @{ path = $mywhooshPath }
    $config | ConvertTo-Json | Set-Content -Path $configFile
}

Write-Host "Found $myWhooshApp at $mywhooshPath"

Start-Process -FilePath $mywhooshPath

# Wait for the application to finish
Write-Host "Waiting for $myWhooshApp to finish..."
while ($process = ps -ax | grep -i $myWhooshApp | grep -v "grep") {
    Write-Output $process
    Start-Sleep -Seconds 5
}

# Run the Python script
Write-Host "$myWhooshApp has finished, running Python script..."
uv run "<PATH_WHERE_YOUR_SCRIPT_IS_LOCATED>/MyWhoosh2Garmin/myWhoosh2Garmin.py"
```

AppleScript (need to test further)

```applescript
TODO: needs more work
```

<h3>Windows</h3>

Windows .ps1 (PowerShell) file (Untested on Windows)
```powershell
# Define the JSON config file path
$configFile = "$PSScriptRoot\mywhoosh_config.json"

# Check if the JSON file exists and read the stored path
if (Test-Path $configFile) {
    $config = Get-Content -Path $configFile | ConvertFrom-Json
    $mywhooshPath = $config.path
} else {
    $mywhooshPath = $null
}

# Validate the stored path
if (-not $mywhooshPath -or -not (Test-Path $mywhooshPath)) {
    Write-Host "Searching for mywhoosh.exe..."
    $mywhooshPath = Get-ChildItem -Path "C:\PROGRAM FILES" -Filter "mywhoosh.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1

    if (-not $mywhooshPath) {
        Write-Host "mywhoosh.exe not found!"
        exit 1
    }

    $mywhooshPath = $mywhooshPath.FullName

    # Store the path in the JSON file
    $config = @{ path = $mywhooshPath }
    $config | ConvertTo-Json | Set-Content -Path $configFile
}

Write-Host "Found mywhoosh.exe at $mywhooshPath"

# Start mywhoosh.exe
Start-Process -FilePath $mywhooshPath

# Wait for the application to finish
Write-Host "Waiting for mywhoosh to finish..."
while (Get-Process -Name "mywhoosh" -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 5
}

# Run the Python script
Write-Host "mywhoosh has finished, running Python script..."
uv run "C:\Path\to\myWhoosh2Garmin.py"
```

<h2>📱 Android</h2>

The script runs in <a href="https://termux.dev">Termux</a>, started from a home-screen icon that opens a window with the log. Because rides come from the MyWhoosh cloud, it works whichever device you rode on.

<p>1. Install <b>Termux</b> and <b>Termux:Widget</b> from <a href="https://f-droid.org/packages/com.termux/">F-Droid</a>. Take both from the same source: Termux plugins only work with a Termux installed from the same place.</p>

<p>2. In Termux, clone the repository and run the setup:</p>

```
pkg install git
git clone https://github.com/B3nToRide/MyWhoosh2Garmin.git
cd MyWhoosh2Garmin
bash android/setup.sh
```

The setup installs Python and the pinned dependencies into `.venv`, asks for your MyWhoosh email and password and stores them in `.env`, and adds the `MyWhoosh2Garmin` shortcut. The first time, it compiles `pydantic-core` with Rust, which takes 10-15 minutes. It then runs a first sync, where Garmin Connect asks for your login once.

<p>3. Put the icon on the home screen. In Termux, run:</p>

```
am start -n com.termux.widget/.TermuxCreateShortcutActivity
```

Tap <b>MyWhoosh2Garmin</b> in the list and confirm when the launcher asks to add it to the home screen. It appears as a normal app icon, drawn from <code>android/icon.png</code>. This works in launchers where dragging a shortcut out does not, such as Smart Launcher. The same script also shows as a text entry in the Termux:Widget list widget and, after tapping <b>CREATE SHORTCUTS</b> in the Termux:Widget app, in the menu when long-pressing its app icon. A shortcut keeps the icon it was created with, so after changing the icon, remove it and create it again.

<p>4. After a ride, close MyWhoosh (swipe it away from recent apps) and tap the icon. The window shows the log and stays open until you press a key.</p>

To update, run `git pull` in the checkout, then `bash android/setup.sh` again. It keeps your `.env` and only installs what changed.

`android/requirements.txt` pins the same versions as `myWhoosh2Garmin.py.lock`, with hashes, because `uv` has no Android build. Docker shares this dependency list. After changing the dependencies, regenerate it with:

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

`MYWHOOSH_DATA_DIR` moves runtime files (session, locks, schedule state and default
backups) out of the checkout. `MYWHOOSH_BACKUP_DIR` overrides the backup location.
`MYWHOOSH_EMAIL_FILE` and `MYWHOOSH_PASSWORD_FILE` read credentials from files and
take precedence over direct values. Only credential settings are loaded from
the script's `.env`; other runtime settings must be actual environment variables.

Exit codes: **0** success/nothing new; **1** failed sync or invalid configuration;
**2** invalid CLI arguments; **75** another instance holds the lock. Native runs
also keep a rotating log (5 MiB × 4 files including the current file). Set
`LOG_TO_FILE=0` for console-only logging, as in Docker.

## Tests

No real credentials or service connections are used:

```sh
uv run --python 3.13 --with-requirements android/requirements.txt --with-requirements tests/requirements.txt python -m unittest discover -s tests -v
```

The optional [Strava tool](strava/README.md) is separate and is not included in
the Docker sync pipeline.

<h2>💻 Built with</h2>

Technologies used in the project:

* Neovim
*   <a href="https://github.com/matin/garth">Garth</a>
*   <a href="https://github.com/marcelorodrigo/mywhoosh-to-garmin">mywhoosh-to-garmin</a>, for the MyWhoosh cloud API endpoints
*   <a href="https://bitbucket.org/stagescycling/fit_tool/src/main/">Fit\_tool</a>
