"""Compose integration check with disposable fake secrets and no network.

Build mywhoosh2garmin:local first, then run: python3 tests/docker_smoke.py
Nothing is deployed persistently and no real accounts are accessed.
"""

import os
import subprocess
import tempfile
import uuid
from pathlib import Path


CONTAINER_CHECK = r'''
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

assert os.geteuid() != 0, "The service must not run as root"
assert os.statvfs("/").f_flag & os.ST_RDONLY, "The root filesystem must be read-only"
assert os.statvfs("/run/secrets/mywhoosh_password").f_flag & os.ST_RDONLY
assert Path("/run/secrets/mywhoosh_password").read_text() == "fake $password'\\ only"
assert not Path("/app/.env").exists()
assert not Path("/app/.git").exists()
state_file = Path("/data/schedule.json")
command = [sys.executable, "/app/myWhoosh2Garmin.py", "--schedule"]

def start():
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def stop(process):
    process.terminate()
    output, _ = process.communicate(timeout=15)
    assert process.returncode == 0, output
    return output

process = start()
try:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if state_file.exists():
            state = json.loads(state_file.read_text())
            if state.get("last_exit_code") == 1 and state["attempts"] == 0:
                break
        if process.poll() is not None:
            raise AssertionError(process.communicate()[0])
        time.sleep(0.1)
    else:
        raise AssertionError("Scheduler did not complete bounded retries")
finally:
    output = stop(process)

assert output.count("Running sync for slot") == 2, output
assert "No Garmin session; run --login" in output, output
assert "fake $password" not in output
assert "last_success" not in state
assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

# No missed slot: a restart must not trigger another sync.
process = start()
time.sleep(1)
output = stop(process)
assert "Running sync for slot" not in output, output
assert "Scheduler started" in output, output
print("Compose smoke test OK: non-root, private mounted secrets, read-only root,")
print("network disabled, bounded retries, clean shutdown and persistent restart.")
'''


def main():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="mywhoosh-smoke-") as directory:
        work = Path(directory)
        data = work / "data"
        data.mkdir(mode=0o700)
        for name, value in (("email", "fake@example.invalid"),
                            ("password", "fake $password'\\ only")):
            target = work / name
            target.write_text(value)
            target.chmod(0o600)
        override = work / "override.yaml"
        override.write_text("services:\n  sync:\n    network_mode: none\n")
        env = {**os.environ,
               "CONTAINER_UID": str(os.getuid()), "CONTAINER_GID": str(os.getgid()),
               "MYWHOOSH_EMAIL_SECRET_FILE": str(work / "email"),
               "MYWHOOSH_PASSWORD_SECRET_FILE": str(work / "password"),
               "SCHEDULE_MODE": "interval", "SCHEDULE_INTERVAL": "4h",
               "SYNC_MAX_ATTEMPTS": "2", "SYNC_RETRY_DELAY_SECONDS": "1"}
        command = ["docker", "compose", "-f", str(root / "compose.yaml"),
                   "-f", str(override), "--project-directory", str(work),
                   "-p", "mywhoosh-smoke-" + uuid.uuid4().hex[:8],
                   "run", "--rm", "--no-deps", "--entrypoint", "python",
                   "sync", "-c", CONTAINER_CHECK]
        subprocess.run(command, env=env, check=True, timeout=60)


if __name__ == "__main__":
    main()
