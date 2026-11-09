#!/usr/bin/env python3
# /// script
# requires-python = "==3.13.*"
# dependencies = [
#     "garth==0.5.2",
#     "fit_tool==0.9.16",
#     "requests>=2.32",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Script name: myWhoosh2Garmin.py
Usage: "uv run myWhoosh2Garmin.py [--local]"
Description:    Downloads the recent rides from the MyWhoosh cloud that are
                not on Garmin Connect yet, or with
                --local reads MyNewActivity-<myWhooshVersion>.fit/.gpx from
                the app folder
                Adds avg power and heartrade
                Removes temperature
                Saves a backup copy and uploads it to Garmin Connect
Credits:        mywhoosh-to-garmin by marcelorodrigo - for the MyWhoosh
                cloud API endpoints.
                https://github.com/marcelorodrigo/mywhoosh-to-garmin
                Garth by matin - for authenticating and uploading with
                Garmin Connect.
                https://github.com/matin/garth
                Fit_tool by mtucker - for parsing the fit file.
                https://bitbucket.org/stagescycling/python_fit_tool.git/src
                mw2gc by embeddedc - used as an example to fix the avg's.
                https://github.com/embeddedc/mw2gc
"""
import argparse
import os
import json
import math
import sys
import logging
from logging.handlers import RotatingFileHandler
import tempfile
import uuid
import xml.etree.ElementTree as ET
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

from runtime import LockBusy, UserError, atomic_target, process_lock, write_json

import garth
import requests
from dotenv import dotenv_values
from garth.exc import GarthException, GarthHTTPError
from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.record_message import (
    RecordMessage,
    RecordTemperatureField
)
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.profile_type import (
    Activity,
    Event,
    EventType,
    FileType,
    GarminProduct,
    LapTrigger,
    Manufacturer,
    SessionTrigger,
    Sport,
    SubSport
)


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MYWHOOSH_DATA_DIR", SCRIPT_DIR)).expanduser()
json_file_path = DATA_DIR / "backup_path.json"
logger = logging.getLogger(__name__)


TOKENS_PATH = DATA_DIR / '.garth'
ENV_PATH = SCRIPT_DIR / '.env'
MYWHOOSH_LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
MYWHOOSH_ACTIVITIES_URL = ("https://service14.mywhoosh.com"
                           "/v2/rider/profile/activities")
MYWHOOSH_DOWNLOAD_URL = ("https://service14.mywhoosh.com"
                         "/v2/rider/profile/download-activity-file")
GARMIN_ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
# Garmin records the same start time as MyWhoosh to the second; the margin
# only absorbs rounding.
SAME_RIDE_TOLERANCE = timedelta(seconds=60)
# Only rides this recent are caught up, so old history is never queried.
CATCH_UP_WINDOW = timedelta(days=21)
MYWHOOSH_ACTIVITY_LIMIT = 10
OUTPUT_MANUFACTURER = Manufacturer.GARMIN.value
OUTPUT_PRODUCT = GarminProduct.EDGE_1040.value
OUTPUT_DEVICE_NAME = "Edge 1040"
# Fix for https://github.com/JayQueue/MyWhoosh2Garmin/issues/2
MYWHOOSH_PREFIX_WINDOWS = "MyWhooshTechnologyService."
GPX_NS = "{http://www.topografix.com/GPX/1/1}"
TPX_NS = "{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}"
EARTH_RADIUS_M = 6371000.0


def configure_logging():
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handlers = [logging.StreamHandler(sys.stdout)]
    if os.environ.get("LOG_TO_FILE", "1") != "0":
        handlers.append(RotatingFileHandler(
            DATA_DIR / "myWhoosh2Garmin.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8"
        ))
    for handler in handlers:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        ))
        logger.addHandler(handler)


def get_fitfile_location() -> Path:
    """
    Get the location of the FIT file directory based on the operating system.

    Returns:
        Path: The path to the FIT file directory.

    Raises:
        UserError: If the path is missing or the platform needs an explicit path.
    """
    configured = os.environ.get("MYWHOOSH_LOCAL_DIR")
    if configured:
        target_path = Path(configured).expanduser()
        if not target_path.is_dir():
            raise UserError("MYWHOOSH_LOCAL_DIR must be an existing directory")
        return target_path
    if sys.platform == "darwin":
        target_path = (
           Path.home()
           / "Library"
           / "Containers"
           / "com.whoosh.whooshgame"
           / "Data"
           / "Library"
           / "Application Support"
           / "Epic"
           / "MyWhoosh"
           / "Content"
           / "Data"
        )
        if target_path.is_dir():
            return target_path
        raise UserError("MyWhoosh export directory not found; set MYWHOOSH_LOCAL_DIR")
    elif os.name == "nt":
        base_path = Path.home() / "AppData" / "Local" / "Packages"
        if base_path.is_dir():
            for directory in sorted(base_path.glob(f"{MYWHOOSH_PREFIX_WINDOWS}*")):
                target_path = directory / "LocalCache/Local/MyWhoosh/Content/Data"
                if target_path.is_dir():
                    return target_path
        raise UserError("No valid MyWhoosh export directory found; set MYWHOOSH_LOCAL_DIR")
    else:
        raise UserError("Set MYWHOOSH_LOCAL_DIR to use --local on this platform")


def get_backup_path(json_file=json_file_path) -> Path:
    """Resolve the configured folder, retaining support for legacy JSON config."""
    configured = os.environ.get("MYWHOOSH_BACKUP_DIR")
    if configured:
        backup_path = Path(configured).expanduser()
    elif os.path.exists(json_file):
        with open(json_file, 'r') as f:
            stored = json.load(f).get('backup_path')
        if not stored or not Path(stored).is_dir():
            raise UserError("Invalid backup path stored in backup_path.json")
        backup_path = Path(stored)
    else:
        backup_path = DATA_DIR / "backups"
    backup_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return backup_path


def save_garmin_session():
    """Persist refreshed tokens without exposing partially written files."""
    TOKENS_PATH.mkdir(parents=True, exist_ok=True, mode=0o700)
    TOKENS_PATH.chmod(0o700)
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as directory:
        garth.save(directory)
        for source in Path(directory).iterdir():
            with atomic_target(TOKENS_PATH / source.name) as target:
                target.write_bytes(source.read_bytes())


def get_credentials_for_garmin():
    """
    Prompt the user for Garmin credentials and authenticate using Garth.

    Returns:
        None

    Exits:
        Exits with status 1 if authentication fails.
    """
    username = input("Username: ")
    password = getpass("Password: ")
    logger.info("Authenticating...")
    try:
        garth.login(username, password)
        save_garmin_session()
        print()
        logger.info("Successfully authenticated!")
    except GarthHTTPError:
        raise UserError("Garmin login failed; check credentials, MFA and service availability") from None


def authenticate_to_garmin(interactive=True):
    """
    Authenticate the user to Garmin by checking for existing tokens and
    resuming the session, or prompting for credentials if no session
    exists or the session is expired.

    Returns:
        None

    Exits:
        Exits with status 1 if authentication fails.
    """
    try:
        if TOKENS_PATH.exists():
            garth.resume(TOKENS_PATH)
            try:
                _ = garth.client.username  # Also checks/refreshes the resumed session.
                save_garmin_session()
                logger.info("Authenticated to Garmin Connect.")
            except GarthException:
                if not interactive:
                    raise UserError("Garmin session expired; run --login interactively") from None
                logger.info("Session expired. Re-authenticating...")
                get_credentials_for_garmin()
        else:
            if not interactive:
                raise UserError("No Garmin session; run --login interactively first")
            logger.info("No existing session. Please log in.")
            get_credentials_for_garmin()
    except GarthException:
        raise UserError("Garmin authentication failed; run --login interactively") from None


def calculate_avg(values) -> float:
    """
    Calculate the average of a list of values, returning 0 if the list is empty.

    Args:
        values (List[float]): The list of values to average.

    Returns:
        float: The average value or 0 if the list is empty.
    """
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else 0


def append_value(values: List[int], message: object, field_name: str) -> None:
    """
    Appends a value to the 'values' list based on a field from 'message'.

    Args:
        values (List[int]): The list to append the value to.
        message (object): The object that holds the field value.
        field_name (str): The name of the field to retrieve from the message.

    Returns:
        None
    """
    value = getattr(message, field_name, None)
    if value is not None:
        values.append(value)


def reset_values() -> tuple[List[int], List[int], List[int]]:
    """Return empty cadence, power and heart-rate collections."""
    return [], [], []


def set_fit_value(message, name, value):
    """Decoded FIT fields have fixed wire sizes; permit adding missing fields."""
    field = message.get_field_by_name(name)
    if field is not None:
        field.growable = True
    setattr(message, name, value)


def set_output_device(message):
    """Identify the recording device, not any attached sensors, as an Edge 1040."""
    set_fit_value(message, "manufacturer", OUTPUT_MANUFACTURER)
    set_fit_value(message, "product", OUTPUT_PRODUCT)
    if isinstance(message, DeviceInfoMessage):
        set_fit_value(message, "product_name", OUTPUT_DEVICE_NAME)


def primary_device_info(timestamp=None):
    message = DeviceInfoMessage()
    message.device_index = 0
    if timestamp is not None:
        message.timestamp = timestamp
    set_output_device(message)
    return message


def cleanup_fit_file(fit_file_path: Path, new_file_path: Path) -> None:
    """
    Clean up the FIT file by processing and removing unnecessary fields.
    Also, calculate average values for cadence, power, and heart rate.

    Args:
        fit_file_path (Path): The path to the input FIT file.
        new_file_path (Path): The path to save the processed FIT file.

    Returns:
        None
    """
    builder = FitFileBuilder()
    fit_file = FitFile.from_file(str(fit_file_path))
    cadence_values, power_values, heart_rate_values = reset_values()
    has_primary_device = any(isinstance(r.message, DeviceInfoMessage)
                             and r.message.device_index == 0 for r in fit_file.records)

    for record in fit_file.records:
        message = record.message
        is_primary_device = isinstance(message, DeviceInfoMessage) and message.device_index == 0
        if isinstance(message, (FileIdMessage, RecordMessage, SessionMessage)) or is_primary_device:
            # Rebuild wire definitions from the edited fields. Keeping the
            # decoded definition would silently discard newly added averages.
            message.definition_message = None
        if isinstance(message, FileIdMessage):
            # Override manufacturer/product but keep other fields
            set_output_device(message)
        if is_primary_device:
            set_output_device(message)
        if isinstance(message, RecordMessage):
            message.remove_field(RecordTemperatureField.ID)
            append_value(cadence_values, message, "cadence")
            append_value(power_values, message, "power")
            append_value(heart_rate_values, message, "heart_rate")
        if isinstance(message, SessionMessage):
            if message.avg_cadence is None and cadence_values:
                set_fit_value(message, "avg_cadence", calculate_avg(cadence_values))
            if message.avg_power is None and power_values:
                set_fit_value(message, "avg_power", calculate_avg(power_values))
            if message.avg_heart_rate is None and heart_rate_values:
                set_fit_value(message, "avg_heart_rate", calculate_avg(heart_rate_values))
            cadence_values, power_values, heart_rate_values = reset_values()
        builder.add(message)
        if isinstance(message, FileIdMessage) and not has_primary_device:
            builder.add(primary_device_info(message.time_created))
            has_primary_device = True
    with atomic_target(new_file_path) as target:
        builder.build().to_file(str(target))
    logger.info(f"Cleaned-up file saved as {new_file_path}")


def haversine_distance(start: tuple, end: tuple) -> float:
    """
    Great-circle distance in metres between two (lat, lon) pairs in degrees.

    Args:
        start (tuple): The (latitude, longitude) of the first point.
        end (tuple): The (latitude, longitude) of the second point.

    Returns:
        float: The distance in metres.
    """
    lat1, lon1 = start
    lat2, lon2 = end
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def parse_gpx_trackpoints(gpx_file_path: Path) -> List[dict]:
    """
    Read the trackpoints out of a MyWhoosh .gpx export.

    MyWhoosh writes power as a bare <power> element inside <extensions>
    rather than the namespaced <gpxtpx:pwr>, so it is read from the GPX
    namespace. Heart rate, cadence and temperature use the Garmin
    TrackPointExtension namespace.

    Args:
        gpx_file_path (Path): The path to the .gpx file.

    Returns:
        List[dict]: One dict per trackpoint, in file order.
    """
    root = ET.parse(gpx_file_path).getroot()
    trackpoints = []

    for point in root.iter(f"{GPX_NS}trkpt"):
        time_element = point.find(f"{GPX_NS}time")
        if time_element is None:
            continue

        timestamp = datetime.fromisoformat(
            time_element.text.strip().replace("Z", "+00:00")
        )
        elevation = point.find(f"{GPX_NS}ele")
        extensions = point.find(f"{GPX_NS}extensions")
        power = None if extensions is None else extensions.find(f"{GPX_NS}power")
        track_point = (None if extensions is None
                       else extensions.find(f"{TPX_NS}TrackPointExtension"))

        def reading(tag, track_point=track_point):
            if track_point is None:
                return None
            element = track_point.find(f"{TPX_NS}{tag}")
            return None if element is None else element.text

        trackpoints.append({
            "timestamp": timestamp,
            "latitude": float(point.get("lat")),
            "longitude": float(point.get("lon")),
            "altitude": float(elevation.text) if elevation is not None else 0.0,
            "power": round(float(power.text)) if power is not None else None,
            "heart_rate": int(reading("hr")) if reading("hr") is not None else None,
            "cadence": int(reading("cad")) if reading("cad") is not None else None,
        })

    return trackpoints


def convert_gpx_to_fit(gpx_file_path: Path, new_file_path: Path) -> None:
    """
    Build an activity .fit file from a MyWhoosh .gpx export.

    Distance and speed are not recorded per trackpoint in the .gpx, so both
    are derived from the distance between consecutive points. Temperature is
    dropped, matching cleanup_fit_file. Averages are computed here because
    they are the values Garmin Connect would otherwise show as empty.

    Args:
        gpx_file_path (Path): The path to the .gpx file to convert.
        new_file_path (Path): The path to save the built .fit file to.

    Returns:
        None

    Raises:
        ValueError: If the .gpx file holds no trackpoints.
    """
    trackpoints = parse_gpx_trackpoints(gpx_file_path)
    if not trackpoints:
        raise ValueError(f"No trackpoints found in {gpx_file_path.name}")
    if any(b["timestamp"] <= a["timestamp"]
           for a, b in zip(trackpoints, trackpoints[1:])):
        raise ValueError("GPX timestamps must be strictly increasing")

    builder = FitFileBuilder(auto_define=True)
    start_time = round(trackpoints[0]["timestamp"].timestamp() * 1000)
    end_time = round(trackpoints[-1]["timestamp"].timestamp() * 1000)

    file_id_message = FileIdMessage()
    file_id_message.type = FileType.ACTIVITY
    set_output_device(file_id_message)
    file_id_message.time_created = start_time
    builder.add(file_id_message)
    builder.add(primary_device_info(start_time))

    cadence_values, power_values, heart_rate_values = [], [], []
    speed_values = []
    distance = 0.0
    previous = None

    for point in trackpoints:
        timestamp = round(point["timestamp"].timestamp() * 1000)
        speed = 0.0
        if previous is not None:
            step = haversine_distance(
                (previous["latitude"], previous["longitude"]),
                (point["latitude"], point["longitude"])
            )
            elapsed = (point["timestamp"] - previous["timestamp"]).total_seconds()
            distance += step
            speed = step / elapsed if elapsed > 0 else 0.0

        record_message = RecordMessage()
        record_message.timestamp = timestamp
        record_message.position_lat = point["latitude"]
        record_message.position_long = point["longitude"]
        record_message.altitude = point["altitude"]
        record_message.distance = distance
        record_message.speed = speed
        for field in ("power", "heart_rate", "cadence"):
            if point[field] is not None:
                setattr(record_message, field, point[field])
        builder.add(record_message)

        for field, values in (("cadence", cadence_values), ("power", power_values),
                              ("heart_rate", heart_rate_values)):
            if point[field] is not None:
                values.append(point[field])
        speed_values.append(speed)
        previous = point

    elapsed_time = (trackpoints[-1]["timestamp"]
                    - trackpoints[0]["timestamp"]).total_seconds()
    average_speed = distance / elapsed_time if elapsed_time else 0.0

    lap_message = LapMessage()
    lap_message.message_index = 0
    lap_message.timestamp = end_time
    lap_message.start_time = start_time
    lap_message.total_elapsed_time = elapsed_time
    lap_message.total_timer_time = elapsed_time
    lap_message.total_distance = distance
    lap_message.avg_speed = average_speed
    lap_message.max_speed = max(speed_values)
    lap_message.event = Event.LAP
    lap_message.event_type = EventType.STOP
    lap_message.lap_trigger = LapTrigger.SESSION_END
    lap_message.sport = Sport.CYCLING
    for field, values in (("power", power_values), ("cadence", cadence_values),
                          ("heart_rate", heart_rate_values)):
        if values:
            setattr(lap_message, f"avg_{field}", calculate_avg(values))
            setattr(lap_message, f"max_{field}", max(values))
    builder.add(lap_message)

    session_message = SessionMessage()
    session_message.message_index = 0
    session_message.timestamp = end_time
    session_message.start_time = start_time
    session_message.total_elapsed_time = elapsed_time
    session_message.total_timer_time = elapsed_time
    session_message.total_distance = distance
    session_message.avg_speed = average_speed
    session_message.max_speed = max(speed_values)
    for field, values in (("power", power_values), ("cadence", cadence_values),
                          ("heart_rate", heart_rate_values)):
        if values:
            setattr(session_message, f"avg_{field}", calculate_avg(values))
            setattr(session_message, f"max_{field}", max(values))
    session_message.first_lap_index = 0
    session_message.num_laps = 1
    session_message.sport = Sport.CYCLING
    session_message.sub_sport = SubSport.VIRTUAL_ACTIVITY
    session_message.event = Event.SESSION
    session_message.event_type = EventType.STOP
    session_message.trigger = SessionTrigger.ACTIVITY_END
    builder.add(session_message)

    activity_message = ActivityMessage()
    activity_message.timestamp = end_time
    activity_message.total_timer_time = elapsed_time
    activity_message.num_sessions = 1
    activity_message.type = Activity.MANUAL
    activity_message.event = Event.ACTIVITY
    activity_message.event_type = EventType.STOP
    builder.add(activity_message)

    with atomic_target(new_file_path) as target:
        builder.build().to_file(str(target))
    logger.info(f"Converted {gpx_file_path.name} to {new_file_path.name} "
                f"({len(trackpoints)} records, {distance:.0f} m).")


def get_most_recent_gpx_file(fitfile_location: Path) -> Optional[Path]:
    """Return the GPX export most recently written, irrespective of version."""
    return max(fitfile_location.glob("MyNewActivity-*.gpx"),
               key=lambda f: (f.stat().st_mtime_ns, f.name), default=None)


def get_most_recent_fit_file(fitfile_location: Path) -> Optional[Path]:
    """Return the FIT export most recently written, irrespective of version."""
    return max(fitfile_location.glob("MyNewActivity-*.fit"),
               key=lambda f: (f.stat().st_mtime_ns, f.name), default=None)


def generate_new_filename(fit_file: Path) -> str:
    """Generates a new filename with a timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return f"{fit_file.stem}_{timestamp}.fit"


def resolve_backup_target(filename: str) -> Path:
    """
    Build the path to write into the backup folder.

    Args:
        filename (str): The name of the file to write.

    Returns:
        Path: The path to write to.

    Exits:
        Exits with status 1 if the backup folder has gone missing.
    """
    return get_backup_path() / filename


def get_most_recent_activity_file(fitfile_location: Path) -> Optional[Path]:
    """
    Return the most recently written MyWhoosh export, .fit or .gpx.

    Both can sit in the directory at once: MyWhoosh 6.2.0 wrote a .gpx for
    one ride and a .fit for the next, leaving the older file in place. The
    newer file is the ride that has not been uploaded yet, so preferring
    one extension outright would re-upload a stale activity. A .fit wins
    only when the two carry the same timestamp.

    Args:
        fitfile_location (Path): The directory containing the exports.

    Returns:
        Optional[Path]: The newest export, or None if there is none.
    """
    candidates = [f for f in (get_most_recent_fit_file(fitfile_location),
                              get_most_recent_gpx_file(fitfile_location))
                  if f is not None]
    if not candidates:
        return None
    return max(candidates,
               key=lambda f: (f.stat().st_mtime_ns, f.suffix == ".fit"))


def cleanup_and_save_activity_file(fitfile_location: Path) -> Optional[Path]:
    """
    Clean up the most recent activity file in a directory and save it as a
    .fit file with a timestamped filename.

    A .fit export is cleaned into a new backup; a .gpx export is converted,
    since MyWhoosh does not always write a .fit file.

    Args:
        fitfile_location (Path): The directory containing the exports.

    Returns:
        Optional[Path]: The path to the newly saved .fit file, or None if
        no export is found.

    Exits:
        Exits with status 1 if the path is invalid or the export cannot be
        processed.
    """
    if not fitfile_location.is_dir():
        raise UserError("The local export path must be an existing directory")

    logger.debug(f"Checking for activity files in directory: "
                 f"{fitfile_location}.")
    activity_file = get_most_recent_activity_file(fitfile_location)

    if not activity_file:
        logger.info("No .fit or .gpx files found.")
        return None

    logger.debug(f"Found the most recent activity file: {activity_file.name}.")
    new_file_path = resolve_backup_target(generate_new_filename(activity_file))
    logger.info(f"Cleaning up {new_file_path}.")

    try:
        if activity_file.suffix == ".fit":
            cleanup_fit_file(activity_file, new_file_path)
        else:
            convert_gpx_to_fit(activity_file, new_file_path)
        logger.info(f"Successfully cleaned {activity_file.name} "
                    f"and saved it as {new_file_path.name}.")
        return new_file_path
    except Exception as error:
        raise UserError(f"Failed to process {activity_file.name} ({type(error).__name__})") from None


def get_credentials_for_mywhoosh() -> tuple[str, str]:
    """
    Read the MyWhoosh email and password from .env next to the script.
    Environment variables of the same name take precedence.

    Returns:
        tuple[str, str]: The email and password.

    Exits:
        Exits with status 1 if either is missing.
    """
    values = {**dotenv_values(ENV_PATH, interpolate=False), **os.environ}
    def secret(name):
        filename = values.get(f"{name}_FILE")
        if filename:
            # Preserve spaces and literal $, quotes and backslashes in passwords.
            try:
                return Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
            except (OSError, UnicodeError):
                raise UserError(f"Cannot read {name}_FILE; check the path, permissions and UTF-8 encoding") from None
        return values.get(name)

    email = secret("MYWHOOSH_EMAIL")
    password = secret("MYWHOOSH_PASSWORD")
    if not email or not password:
        raise UserError("Set MYWHOOSH_EMAIL and MYWHOOSH_PASSWORD, or their _FILE paths")
    return email, password


def get_mywhoosh_device_id() -> str:
    """Keep one device identity per data directory, including after failed logins."""
    path = DATA_DIR / "mywhoosh_device_id.json"
    with process_lock(DATA_DIR / "mywhoosh_device.lock"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data["device_id"]
            if not isinstance(value, str):
                raise ValueError
            device_id = str(uuid.UUID(value))
        except FileNotFoundError:
            device_id = str(uuid.uuid4())
            # Persist before contacting MyWhoosh: a refused login or a network
            # timeout must not cause the next attempt to invent another device.
            write_json(path, {"device_id": device_id})
        except (ValueError, KeyError, TypeError):
            raise UserError("Invalid mywhoosh_device_id.json; restore a valid copy "
                            "to preserve the MyWhoosh device identity") from None
        except OSError:
            raise UserError("Cannot read mywhoosh_device_id.json; check data directory permissions") from None
    logger.info("Using persistent MyWhoosh device identity.")
    return device_id


def authenticate_to_mywhoosh(email: str, password: str) -> tuple[str, str]:
    """
    Log in to the MyWhoosh cloud API.

    Reuse this installation's device ID. MyWhoosh may refuse the login if
    another session is still active; this does not identify the other device.

    Args:
        email (str): The MyWhoosh account email.
        password (str): The MyWhoosh account password.

    Returns:
        tuple[str, str]: The access token and the WhooshId.

    Raises:
        UserError: If the device identity is invalid or the login is refused.
    """
    device_id = get_mywhoosh_device_id()
    response = requests.post(MYWHOOSH_LOGIN_URL, json={
        "Username": email,
        "Password": password,
        "Platform": "Android",
        "Action": 1001,
        "CorrelationId": str(uuid.uuid4()),
        "DeviceId": device_id,
        "Authorization": "",
    }, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("Success"):
        message = str(data.get("Message", ""))
        if "already logged in" in message.lower():
            raise UserError("MyWhoosh reports another active session; close any open "
                            "MyWhoosh apps and retry later. A previous session may still be active")
        # Do not log arbitrary authentication responses or request objects.
        raise UserError("MyWhoosh login refused; check credentials and account availability")
    logger.info("Authenticated to MyWhoosh.")
    return data["AccessToken"], data["WhooshId"]


def get_recent_mywhoosh_activities(access_token: str) -> List[dict]:
    """Fetch only the ten newest rides; the caller applies the 21-day cutoff."""
    response = requests.post(
        MYWHOOSH_ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"page": 1, "limit": MYWHOOSH_ACTIVITY_LIMIT, "sortDate": "DESC"},
        timeout=30
    )
    response.raise_for_status()
    activities = response.json()["data"]["results"]
    if not isinstance(activities, list) or any(not isinstance(a, dict) for a in activities):
        raise UserError("MyWhoosh returned an invalid activity page")
    return activities[:MYWHOOSH_ACTIVITY_LIMIT]


def mywhoosh_start_time(activity: dict) -> datetime:
    """Return the ride's start time as an aware UTC datetime."""
    start = datetime.fromisoformat(
        activity["startDatetime"].replace("Z", "+00:00")
    )
    if start.tzinfo is None:
        raise ValueError("MyWhoosh start time is missing its timezone")
    return start.astimezone(timezone.utc)


def get_garmin_start_times(ride_starts: List[datetime]) -> List[datetime]:
    """
    Fetch the start times of the Garmin Connect activities on the days
    the given rides took place.

    Each day is widened by a day on either side, because Garmin filters on
    the local calendar date.

    Args:
        ride_starts (List[datetime]): The ride start times to cover.

    Returns:
        List[datetime]: The start times, as aware UTC datetimes.
    """
    start_times = []
    for day in sorted({start.date() for start in ride_starts}):
        params = {
            "startDate": (day - timedelta(days=1)).isoformat(),
            "endDate": (day + timedelta(days=1)).isoformat(),
            "limit": 100,
        }
        fetched = 0
        while True:
            page = garth.connectapi(GARMIN_ACTIVITIES_PATH,
                                    params={**params, "start": fetched})
            fetched += len(page)
            start_times += [
                datetime.fromisoformat(a["startTimeGMT"])
                .replace(tzinfo=timezone.utc)
                for a in page
                if is_mywhoosh_candidate(a)
            ]
            if len(page) < params["limit"]:
                break
    return start_times


def is_mywhoosh_candidate(activity: dict) -> bool:
    """Do not mistake a simultaneous run/watch recording for a virtual ride."""
    kind = (activity.get("activityType") or {}).get("typeKey", "")
    return (kind in {"virtual_ride", "virtual_cycling"}
            or (kind in {"cycling", "indoor_cycling"}
                and "mywhoosh" in (activity.get("activityName") or "").lower()))


def download_mywhoosh_activity(access_token: str, whoosh_id: str,
                               activity: dict, target: Path) -> None:
    """
    Download an activity's .fit file from the MyWhoosh cloud.

    The API hands back a presigned S3 URL, which is then fetched.

    Args:
        access_token (str): The token returned by authenticate_to_mywhoosh.
        whoosh_id (str): The WhooshId returned by authenticate_to_mywhoosh.
        activity (dict): The activity to download.
        target (Path): Where to write the .fit file.

    Returns:
        None
    """
    response = requests.post(
        MYWHOOSH_DOWNLOAD_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"key": whoosh_id, "fileId": activity["activityFileId"]},
        timeout=30
    )
    response.raise_for_status()
    fit_response = requests.get(response.json()["data"], timeout=60)
    fit_response.raise_for_status()
    target.write_bytes(fit_response.content)


def mywhoosh_activity_filename(activity: dict) -> str:
    """Use UTC to avoid collisions during DST and changes between host zones."""
    start = mywhoosh_start_time(activity)
    return f"MyWhoosh_{start:%Y-%m-%d_%H%M%S}Z.fit"


def sync_cloud_activities() -> bool:
    """Sync oldest first, isolating each ride's errors. Return overall success."""
    email, password = get_credentials_for_mywhoosh()
    access_token, whoosh_id = authenticate_to_mywhoosh(email, password)
    cutoff = datetime.now(timezone.utc) - CATCH_UP_WINDOW
    recent = []
    failed = 0
    for activity in get_recent_mywhoosh_activities(access_token):
        try:
            start = mywhoosh_start_time(activity)
            if start >= cutoff:
                recent.append((activity, start))
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.error("Skipping an activity with invalid start-time metadata.")
            failed += 1
    recent.sort(key=lambda pair: pair[1])
    if not recent:
        logger.info(f"No MyWhoosh activities in the last "
                    f"{CATCH_UP_WINDOW.days} days.")
        return failed == 0

    garmin_starts = get_garmin_start_times([start for _, start in recent])

    succeeded = skipped = 0
    for activity, start in recent:
        label = f"{activity.get('title', 'MyWhoosh')} ({start.isoformat()})"
        if any(abs(start - g) <= SAME_RIDE_TOLERANCE
               for g in garmin_starts):
            logger.info(f"Already on Garmin Connect: {label}.")
            skipped += 1
            continue

        logger.info(f"New MyWhoosh activity: {label}.")
        try:
            new_file_path = resolve_backup_target(
                mywhoosh_activity_filename(activity)
            )
            with tempfile.TemporaryDirectory() as download_dir:
                downloaded = Path(download_dir) / new_file_path.name
                download_mywhoosh_activity(access_token, whoosh_id,
                                           activity, downloaded)
                cleanup_fit_file(downloaded, new_file_path)
            if not upload_fit_file_to_garmin(new_file_path):
                failed += 1
                continue
            garmin_starts.append(start)
            succeeded += 1
        except Exception as error:
            # Continue the batch. Avoid leaking presigned URLs or token-bearing
            # exception bodies into persistent/container logs.
            logger.error("Activity failed (%s); continuing with the next ride.",
                         type(error).__name__)
            failed += 1
    logger.info("Sync complete: %d submitted, %d already present, %d failed.",
                succeeded, skipped, failed)
    return failed == 0


def upload_fit_file_to_garmin(new_file_path: Path) -> bool:
    """
    Upload a .fit file to Garmin using the Garth client.

    Args:
        new_file_path (Path): The path to the .fit file to upload.

    Returns:
        bool: True if Garmin confirmed/acknowledged the upload or already had it,
        False if the upload failed.
    """
    try:
        with open(new_file_path, "rb") as f:
            uploaded = garth.client.upload(f)
        # Garmin can report failures inside HTTP 200, or acknowledge a file with
        # an uploadId and empty result lists (the response documented by garth).
        # An acknowledgement is not proof that the activity is already visible.
        imports = uploaded.get("detailedImportResult") if isinstance(uploaded, dict) else None
        if not isinstance(imports, dict):
            logger.error("Unexpected Garmin upload response for %s: no detailedImportResult object.",
                         new_file_path.name)
            return False
        successes = imports.get("successes")
        failures = imports.get("failures")
        successes = [] if successes is None else successes
        failures = [] if failures is None else failures
        if not isinstance(successes, list) or not isinstance(failures, list):
            logger.error("Unexpected Garmin import result for %s: invalid successes/failures lists.",
                         new_file_path.name)
            return False
        if failures:
            codes = set()
            for failure in failures:
                messages = failure.get("messages", []) if isinstance(failure, dict) else []
                if not isinstance(messages, list):
                    continue
                for message in messages:
                    code = message.get("code") if isinstance(message, dict) else None
                    # Log numeric error codes only, never free-form server text,
                    # which may contain account data, tokens or signed URLs.
                    if type(code) is int and 0 <= code <= 999999:
                        codes.add(str(code))
                    elif isinstance(code, str) and 0 < len(code) <= 6 and code.isascii() and code.isdecimal():
                        codes.add(code)
            logger.error("Garmin rejected import for %s (%d failure(s); codes: %s).",
                         new_file_path.name, len(failures), ", ".join(sorted(codes)) or "not provided")
            return False
        if successes:
            logger.info("Uploaded %s to Garmin Connect.", new_file_path.name)
            return True
        upload_id = imports.get("uploadId")
        if type(upload_id) is int and upload_id > 0:
            logger.info("Garmin accepted %s (upload acknowledged; activity processing may continue).",
                        new_file_path.name)
            return True
        logger.error("Garmin did not acknowledge import for %s "
                     "(no successes, no failures, no valid uploadId).", new_file_path.name)
        return False
    except GarthHTTPError as e:
        response = e.error.response
        if response is not None and response.status_code == 409:
            logger.info("Duplicate activity found on Garmin Connect.")
            return True
        logger.error("Uploading %s failed (HTTP %s).", new_file_path.name,
                     response.status_code if response is not None else "unknown")
        return False


def main(argv=None):
    """
    Main function to authenticate to Garmin, fetch, clean and save the
    FIT file, and upload it to Garmin.

    Exits with status 0 when the run completes, including when there is
    nothing new to upload, and with status 1 when any step fails.

    Returns:
        None
    """
    parser = argparse.ArgumentParser(
        description="Upload recent MyWhoosh rides to Garmin Connect."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="sync once (the default)")
    mode.add_argument("--login", action="store_true", help="authenticate Garmin interactively, without syncing")
    mode.add_argument("--schedule", action="store_true", help="run the persistent daily/interval scheduler")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt for Garmin credentials")
    parser.add_argument(
        "--local", action="store_true",
        help="read the newest export from the MyWhoosh app folder "
             "instead of downloading it from the MyWhoosh cloud"
    )
    args = parser.parse_args(argv)
    if args.local and (args.schedule or args.login):
        parser.error("--local only applies to one-shot synchronisation")
    if args.login and args.non_interactive:
        parser.error("--login requires interactive input")

    try:
        os.umask(0o077)
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        configure_logging()
    except OSError:
        print("Cannot create runtime files; check MYWHOOSH_DATA_DIR permissions.", file=sys.stderr)
        return 1
    try:
        if args.schedule:
            from scheduler import serve
            return serve(DATA_DIR, SCRIPT_DIR / "myWhoosh2Garmin.py", logger.handlers)
        with process_lock(DATA_DIR / "sync.lock"):
            if args.login:
                get_credentials_for_garmin()
                return 0
            authenticate_to_garmin(interactive=not args.non_interactive and sys.stdin.isatty())
            try:
                if args.local:
                    path = cleanup_and_save_activity_file(get_fitfile_location())
                    success = path is None or upload_fit_file_to_garmin(path)
                else:
                    success = sync_cloud_activities()
                return 0 if success else 1
            finally:
                save_garmin_session()
    except LockBusy:
        logger.warning("Another instance is running; retry later.")
        return 75
    except UserError as error:
        logger.error("%s", error)
        return 1
    except (requests.RequestException, GarthException) as error:
        logger.error("Service request failed (%s). Retry later; if persistent, run --login.",
                     type(error).__name__)
        return 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Only our own validation messages are safe to expose. Third-party
        # exceptions can contain server responses, tokens or signed URLs.
        logger.error("Configuration, session or activity data error (%s). Check configuration; Garmin may need --login.",
                     type(error).__name__)
        return 1
    except Exception as error:
        logger.error("Synchronisation failed (%s); inspect the input or report the issue.",
                     type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
