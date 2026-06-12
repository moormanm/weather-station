#!/usr/bin/env bash


# Hide mouse cursor immediately
unclutter -idle 0 &

# Disable all X11 screen blanking / DPMS
xset s off
xset s noblank
xset -dpms

# Wayfire / labwc (Bookworm Wayland) — belt-and-suspenders
if command -v wlr-randr &>/dev/null; then
    wlr-randr --output HDMI-A-1 --on 2>/dev/null || true
fi

# Launch weather station
cd "/home/pi/weather_station"
# get latest
git pull

exec /usr/bin/python3 "/home/pi/weather_station/weather_station/weather_station.py"
