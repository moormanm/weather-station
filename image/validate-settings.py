import math
import sys
from configparser import ConfigParser, Error

try:
    config = ConfigParser()
    with open(sys.argv[1]) as source:
        config.read_file(source)
    for key, limit in (("LATITUDE", 90), ("LONGITUDE", 180)):
        value = config.getfloat("LOCATION", key)
        if not math.isfinite(value) or not -limit <= value <= limit:
            raise ValueError(f"{key} must be between {-limit} and {limit}.")
    if not config.get("LOCATION", "NAME").strip():
        raise ValueError("LOCATION NAME must not be empty.")
    if config.has_section("FEATURES"):
        for key in config["FEATURES"]:
            config.getboolean("FEATURES", key)
    if config.getint("GENERAL", "SELF_UPDATE_INTERVAL_S", fallback=21600) <= 0:
        raise ValueError("SELF_UPDATE_INTERVAL_S must be greater than zero.")
except (Error, ValueError, OSError) as error:
    sys.exit(f"Invalid settings: {error}")
