#!/usr/bin/env python3
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from configparser import ConfigParser, Error


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

    config = ConfigParser(interpolation=None)
    config.optionxform = str.upper
    with open(path, encoding="utf-8") as source:
        contents = source.read()
    config.read_string(contents)
    if not config.has_section("LOCATION"):
        raise ValueError("The settings file has no LOCATION section.")

    values = {"LATITUDE": str(latitude), "LONGITUDE": str(longitude), "NAME": name}
    lines = contents.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if re.match(r"^\s*\[LOCATION\]", line))
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^\s*\[", lines[i])), len(lines))
    updated = set()
    for i in range(start + 1, end):
        match = re.match(r"^\s*(LATITUDE|LONGITUDE|NAME)\s*[:=]", lines[i], re.IGNORECASE)
        if match:
            key = match.group(1).upper()
            lines[i] = f"{key}={values[key]}\n"
            updated.add(key)
    for key in values:
        if key not in updated:
            if end and not lines[end - 1].endswith(("\n", "\r")):
                lines[end - 1] += "\n"
            lines.insert(end, f"{key}={values[key]}\n")
            end += 1

    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".ws-settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.writelines(lines)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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


def choose_location():
    while True:
        method = input(
            "\nEnter A to look up a U.S. street address, or M to enter coordinates: "
        ).strip().lower()
        if method == "m":
            return (input("Latitude (-90 to 90): "), input("Longitude (-180 to 180): "))
        if method != "a":
            print("Enter A or M.")
            continue
        print("The address will be sent to the U.S. Census Bureau.")
        if input("Continue? [y/N]: ").strip().lower() != "y":
            continue
        address = input("Street address, city, state, and ZIP: ").strip()
        if not address:
            print("Enter an address.")
            continue
        try:
            results = lookup_address(address)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Address lookup failed: {error}")
            print("Try again or enter coordinates manually.")
            continue
        if not results:
            print("No address matched. Try again or enter coordinates manually.")
            continue
        for index, (matched, latitude, longitude) in enumerate(results, 1):
            print(f"{index}. {matched} (latitude {latitude}, longitude {longitude})")
        selection = input("Enter a match number, or press Enter to cancel: ").strip()
        if not selection.isdecimal() or not 1 <= int(selection) <= len(results):
            print("No match selected. Try again or enter coordinates manually.")
            continue
        _, latitude, longitude = results[int(selection) - 1]
        if input("Use these approximate coordinates? [y/N]: ").strip().lower() == "y":
            return latitude, longitude


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--interactive":
        name = input("Station name (for example, City, State): ")
        latitude, longitude = choose_location()
        save_location(sys.argv[2], name, latitude, longitude)
    elif len(sys.argv) == 5:
        save_location(*sys.argv[1:])
    else:
        raise ValueError("Expected --interactive and settings path, or path, name, latitude, and longitude.")


if __name__ == "__main__":
    try:
        main()
    except (Error, OSError, ValueError) as error:
        sys.exit(f"Could not save location: {error}")
