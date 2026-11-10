# Legacy AppleScript launcher — optional, not live-validated

For scheduled automation, use the [Docker guide](../docs/docker.md). This
upstream-derived launcher is retained for people who want a **native macOS sync
after quitting MyWhoosh**, not a daily or elapsed-time scheduler. It is not used
by Docker and has not been live-tested as part of this fork.

## Prerequisites

1. Follow the [native installation](../README.md#native-installation) using the
   complete checkout. Downloading only `myWhoosh2Garmin.py` is insufficient.
2. Configure MyWhoosh credentials in `.env` next to the Python script.
3. Run `uv run --locked myWhoosh2Garmin.py --login` in an interactive terminal,
   including MFA if required, then test `--once --non-interactive` manually.
4. Run `command -v uv` and note its absolute path. Do not assume it is installed
   at `$HOME/.local/bin/uv`.

`do shell script` has no interactive terminal and does not inherit the runtime
variables exported in your Terminal session. Use the same data-directory
configuration for login and the launcher; otherwise it will not find the Garmin
session. If that session expires, re-authenticate in Terminal, not in the app.

## Adapt the launcher before exporting it

Open `MyWhoosh2Garmin-AS.scpt` in Script Editor. The bundled compiled script is
unchanged and contains the older invocation: **replace its source with the
example below**, then set the application name and both absolute paths.

```applescript
property targetApp : "MyWhoosh Indoor Cycling App"
property uvExecutablePath : "/absolute/path/to/uv"
property pythonScriptPath : "/absolute/path/to/MyWhoosh2Garmin/myWhoosh2Garmin.py"
property appRunning : false

on idle
	if application targetApp is running then
		set appRunning to true
	else if appRunning then
		set appRunning to false
		performActionOnExit()
	end if
	return 30 -- Check every 30 seconds
end idle

on performActionOnExit()
	do shell script (quoted form of uvExecutablePath) & " run --locked " & (quoted form of pythonScriptPath) & " --once --non-interactive"
end performActionOnExit

on quit
	continue quit
end quit
```

Export it as an **Application**, selecting **Stay open after run handler**. Launch
that app before starting MyWhoosh. It checks every 30 seconds and submits one
cloud sync after it has observed MyWhoosh running and then quitting.

## Permissions and limitations

- Full Disk Access is **not a blanket prerequisite** for cloud sync. Grant only
  the specific automation or folder permissions macOS requests for your chosen
  paths; do not grant broad disk access merely because an older guide said so.
- There is no credential prompt, retry queue, persisted exit-monitor state or
  catch-up of missed application-exit events in this launcher. Docker scheduling
  is the documented alternative for those operational needs.
- The native script still limits cloud catch-up to the 10 latest activities
  within 21 days and uses its shared sync lock.
- Errors can surface through AppleScript rather than a terminal window. Inspect
  the native rotating log in the configured data directory and test the command
  manually before relying on the exported app.
