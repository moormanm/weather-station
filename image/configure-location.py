#!/usr/bin/env python3
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from configparser import ConfigParser, Error


def update_settings(path, changes):
    with open(path, encoding="utf-8") as source:
        contents = source.read()
    config = ConfigParser(interpolation=None)
    config.read_string(contents)
    lines = contents.splitlines(keepends=True)
    for section, values in changes.items():
        if not config.has_section(section):
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(f"\n[{section}]\n")
        start = next(i for i, line in enumerate(lines) if re.match(
            rf"^\s*\[{re.escape(section)}\]", line
        ))
        end = next((i for i in range(start + 1, len(lines))
                    if re.match(r"^\s*\[", lines[i])), len(lines))
        updated = set()
        for i in range(start + 1, end):
            match = re.match(r"^\s*([^#;=:\s]+)\s*[:=]", lines[i])
            if match and match.group(1).upper() in values:
                key = match.group(1).upper()
                lines[i] = f"{key}={values[key]}\n" if values[key] is not None else ""
                updated.add(key)
        for key, value in values.items():
            if key not in updated and value is not None:
                if end and not lines[end - 1].endswith(("\n", "\r")):
                    lines[end - 1] += "\n"
                lines.insert(end, f"{key}={value}\n")
                end += 1
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".ws-settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.writelines(lines)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_location(path, name, latitude_text, longitude_text):
    name = name.strip()
    latitude = float(latitude_text)
    longitude = float(longitude_text)
    if not name:
        raise ValueError("Station name must not be empty.")
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180.")

    update_settings(path, {"LOCATION": {
        "NAME": name, "LATITUDE": str(latitude), "LONGITUDE": str(longitude),
    }})


def lookup_address(address):
    query = urllib.parse.urlencode({
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    })
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + query
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.loads(response.read(1024 * 1024))
    matches = data["result"]["addressMatches"]
    if not isinstance(matches, list):
        raise ValueError("The geocoder response is invalid.")
    results = []
    for match in matches:
        coords = match["coordinates"]
        latitude, longitude = float(coords["y"]), float(coords["x"])
        if not (math.isfinite(latitude) and -90 <= latitude <= 90
                and math.isfinite(longitude) and -180 <= longitude <= 180):
            raise ValueError("The geocoder returned invalid coordinates.")
        address = match["matchedAddress"]
        if not isinstance(address, str) or not address.strip():
            raise ValueError("The geocoder returned an invalid address.")
        results.append((address, latitude, longitude))
    return results


class SetupCancelled(Exception):
    pass


def dialog(*arguments):
    result = subprocess.run(
        ["whiptail", "--title", "Weather Station Setup", *arguments],
        stderr=subprocess.PIPE, text=True,
    )
    if result.returncode == 1 or (result.returncode == 255 and not result.stderr.strip()):
        raise SetupCancelled
    if result.returncode != 0:
        raise OSError(f"Cannot open setup dialog: {result.stderr.strip()}")
    return result.stderr.strip()


def choose_location():
    while True:
        address = dialog(
            "--inputbox",
            "Enter a U.S. street address, city, state, and ZIP.\n\n"
            "The address will be sent to the U.S. Census Bureau.",
            "14", "74",
        )
        if not address:
            dialog("--msgbox", "Enter a street address.", "10", "74")
            continue
        try:
            results = lookup_address(address)
        except (OSError, ValueError, KeyError, TypeError) as error:
            dialog("--msgbox", f"Address lookup failed: {error}\n\nTry again.", "12", "74")
            continue
        if not results:
            dialog("--msgbox", "No address matched. Try again.", "10", "74")
            continue
        if len(results) == 1:
            return results[0][1:]
        items = []
        for index, (matched, _, _) in enumerate(results, 1):
            items.extend((str(index), matched))
        selection = dialog("--menu", "Select the matching address:", "18", "74", "8", *items)
        if not selection.isdecimal() or not 1 <= int(selection) <= len(results):
            raise ValueError("The selected address is invalid.")
        return results[int(selection) - 1][1:]


def detect_timezone(latitude, longitude):
    url = f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "weather-station (https://github.com/moormanm/weather-station)"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read(1024 * 1024))
    timezone = data["properties"]["timeZone"]
    if not isinstance(timezone, str) or not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+", timezone):
        raise ValueError("The weather service returned an invalid time zone.")
    return timezone


def set_timezone(timezone):
    result = subprocess.run(
        ["timedatectl", "--no-ask-password", "set-timezone", timezone],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise OSError(f"Cannot set time zone: {result.stderr.strip() or 'timedatectl failed'}")


def interactive_location():
    while True:
        name = dialog("--inputbox", "Enter a station name (for example, City, State):", "10", "74")
        if name:
            break
        dialog("--msgbox", "Enter a station name.", "10", "74")
    while True:
        latitude, longitude = choose_location()
        try:
            lat, lon = float(latitude), float(longitude)
            if not (math.isfinite(lat) and -90 <= lat <= 90
                    and math.isfinite(lon) and -180 <= lon <= 180):
                raise ValueError
        except ValueError:
            dialog("--msgbox", "Enter a valid latitude and longitude.", "10", "74")
            continue
        return name, lat, lon


def interactive_setup(path):
    config = ConfigParser(interpolation=None)
    with open(path, encoding="utf-8") as source:
        config.read_file(source)
    changes = {}
    try:
        name = config.get("LOCATION", "NAME").strip()
        lat = config.getfloat("LOCATION", "LATITUDE")
        lon = config.getfloat("LOCATION", "LONGITUDE")
        valid_location = bool(name) and math.isfinite(lat) and -90 <= lat <= 90 and math.isfinite(lon) and -180 <= lon <= 180
    except (Error, ValueError):
        valid_location = False
    if valid_location:
        try:
            dialog("--yesno", f"Current station: {name}\n\nChange location?", "10", "74")
        except SetupCancelled:
            pass
        else:
            valid_location = False
    if not valid_location:
        name, lat, lon = interactive_location()
        changes["LOCATION"] = {"NAME": name, "LATITUDE": str(lat), "LONGITUDE": str(lon)}

    features = [
        ("COORDINATE_TIDES", "Nearest tide station", False),
        ("HOURLY_FORECAST", "Hourly forecast", True),
        ("HOURLY_PRECIP", "Hourly precipitation", config.getboolean("FEATURES", "HOURLY_FORECAST", fallback=True)),
        ("ALT_PREDICTIONS", "Alternative predictions", False),
        ("SUNTIME", "Sunrise and sunset", True),
        ("POTOMAC", "Potomac river", False),
    ]
    items = []
    for key, label, default in features:
        items.extend((key, label, "ON" if config.getboolean("FEATURES", key, fallback=default) else "OFF"))
    selected = dialog("--separate-output", "--checklist",
                      "Select features with Space. Press Tab, then Enter to continue.",
                      "20", "74", "8", *items).splitlines()
    changes["FEATURES"] = {key: str(key in selected).lower() for key, _, _ in features}

    while True:
        deployment = dialog("--inputbox", "Deployment ID (optional). Remote values override local settings.",
                            "11", "74", config.get("GENERAL", "DEPLOYMENT_ID", fallback=""))
        if not deployment or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", deployment):
            break
        dialog("--msgbox", "Use letters, numbers, underscores, or hyphens for the deployment ID.", "10", "74")
    while True:
        interval = dialog("--inputbox", "Self-update interval in seconds:",
                          "10", "74", config.get("GENERAL", "SELF_UPDATE_INTERVAL_S", fallback="21600"))
        if interval.isascii() and interval.isdecimal() and int(interval) > 0:
            break
        dialog("--msgbox", "Enter a whole number greater than zero.", "10", "74")
    changes["GENERAL"] = {"DEPLOYMENT_ID": deployment, "SELF_UPDATE_INTERVAL_S": interval}
    try:
        timezone = detect_timezone(lat, lon)
        set_timezone(timezone)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise OSError(f"Time zone setup failed: {error}") from error
    update_settings(path, changes)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--interactive":
        name, lat, lon = interactive_location()
        save_location(sys.argv[2], name, lat, lon)
    elif len(sys.argv) == 3 and sys.argv[1] == "--setup":
        interactive_setup(sys.argv[2])
    elif len(sys.argv) == 5:
        save_location(*sys.argv[1:])
    else:
        raise ValueError("Expected --interactive and settings path, or path, name, latitude, and longitude.")


if __name__ == "__main__":
    try:
        main()
    except SetupCancelled:
        if "--setup" in sys.argv:
            dialog("--msgbox", "Station setup was cancelled. No settings were saved.", "10", "74")
        else:
            print("Station setup cancelled.", file=sys.stderr)
        sys.exit(2)
    except (Error, OSError, ValueError) as error:
        message = f"Station setup failed: {error}"
        print(message, file=sys.stderr)
        if "--setup" in sys.argv:
            try:
                dialog("--msgbox", message, "12", "74")
            except (SetupCancelled, OSError):
                pass
        sys.exit(1)
