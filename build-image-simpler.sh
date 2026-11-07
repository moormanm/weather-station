#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BASE_NAME=2026-10-06-raspios-trixie-arm64-lite.img.xz
BASE_SHA=483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843
BASE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/$BASE_NAME"

usage() {
    cat <<'EOF'
Usage: ./build-image-simpler.sh [--help]

Customize an official 64-bit Raspberry Pi OS Lite image for Raspberry Pi Imager.
Run on the build computer, not on the Pi. Requires Git, Python 3, curl, Docker,
and at least 15 GB of free Docker storage plus 10 GB of free host storage.
Docker must support privileged Linux containers and loop-device mounts.

Environment variables:
  WS_IMAGE_SETTINGS    Settings file (default: ~/ws-settings.ini).
  WS_IMAGE_WALLPAPER   Wallpaper file (default: image/wallpaper.svg).
  WS_IMAGE_REPOSITORY  Public HTTPS Git URL
                       (default: https://github.com/moormanm/weather-station.git).
  WS_IMAGE_REF         Branch or tag (default: main).

The base image is downloaded once and its SHA-256 checksum is checked.
Docker caches the extracted base image and downloaded Debian packages.
Only the small desktop and app dependencies are installed. The OS is not rebuilt.
The first build still needs internet access and time to install these packages.

Output: .image-build/base-*/weather-station.img
Select "Use custom" in Raspberry Pi Imager. Skip OS customisation.
The image includes pi/pi, automatic login, US Wi-Fi, and disabled SSH.
Press Q to show the desktop. It has shortcuts for settings, Wi-Fi, and the app.
Use Ctrl+Alt+E to edit settings, Ctrl+Alt+W for Wi-Fi, or Ctrl+Alt+S to start.
The settings editor and Wi-Fi tool support keyboard input. The app starts
without a Git request.

Your settings are included in the output. Do not share personal settings or secrets.
The app is cloned from GitHub; local uncommitted changes are not included.
EOF
}

if [ "$#" -gt 0 ]; then
    case "$1" in
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
fi

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

for command in git python3 curl docker; do
    command -v "$command" >/dev/null || fail "Install $command before building."
done
[ "$(docker info --format '{{.OSType}}')" = linux ] ||
    fail "Start a Linux Docker engine."
if docker info --format '{{json .SecurityOptions}}' | grep -q rootless; then
    fail "This build needs rootful Docker with privileged containers."
fi
SETTINGS="${WS_IMAGE_SETTINGS:-$HOME/ws-settings.ini}"
WALLPAPER="${WS_IMAGE_WALLPAPER:-$ROOT/image/wallpaper.svg}"
REPOSITORY="${WS_IMAGE_REPOSITORY:-https://github.com/moormanm/weather-station.git}"
REF="${WS_IMAGE_REF:-main}"
[ -f "$SETTINGS" ] || fail "Set WS_IMAGE_SETTINGS to a completed ws-settings.ini file."
[ -f "$WALLPAPER" ] || fail "Wallpaper file not found: $WALLPAPER"
[[ "$REPOSITORY" == https://* ]] && [[ "$REPOSITORY" != *"@"* ]] ||
    fail "Use a public HTTPS Git URL without credentials."

python3 "$ROOT/image/validate-settings.py" "$SETTINGS"

umask 077
mkdir -p "$ROOT/.image-build/cache"
CACHE="$ROOT/.image-build/cache/$BASE_NAME"
if [ ! -f "$CACHE" ]; then
    printf 'Downloading official Raspberry Pi OS Lite (cached for later builds).\n'
    PART="$(mktemp "$CACHE.partial-XXXXXXXX")"
    trap 'rm -f -- "$PART"' EXIT
    curl --fail --location --retry 3 --connect-timeout 20 "$BASE_URL" -o "$PART"
    python3 "$ROOT/image/check-image.py" "$PART" "$BASE_SHA"
    mv "$PART" "$CACHE"
    trap - EXIT
else
    printf 'Using cached official image.\n'
    python3 "$ROOT/image/check-image.py" "$CACHE" "$BASE_SHA"
fi

BUILD="$(mktemp -d "$ROOT/.image-build/base-XXXXXXXX")"
mkdir "$BUILD/files"
cp "$SETTINGS" "$BUILD/files/ws-settings.ini"
cp "$WALLPAPER" "$BUILD/files/wallpaper"
cp "$ROOT/image/"{weather-station-session,weather-station-launch,weather-station-settings,openbox.xml} \
    "$BUILD/files/"
git clone --depth 1 --branch "$REF" --single-branch -- "$REPOSITORY" \
    "$BUILD/files/weather-station"
[ -f "$BUILD/files/weather-station/weather_station.py" ] ||
    fail "The selected repository has no weather_station.py file."
git -C "$BUILD/files/weather-station" rev-parse HEAD > "$BUILD/app-revision"
printf 'Build directory: %s\n' "$BUILD"
docker build -t weather-station-image-tools -f "$ROOT/image/Dockerfile" "$ROOT/image"
docker run --rm --privileged \
    --mount "type=bind,source=$CACHE,target=/base.img.xz,readonly" \
    --mount "type=bind,source=$BUILD,target=/output" \
    --mount "type=volume,source=weather-station-image-cache,target=/cache" \
    --volume /work \
    --env "BASE_SHA=$BASE_SHA" \
    --env "OUTPUT_UID=$(id -u)" --env "OUTPUT_GID=$(id -g)" \
    weather-station-image-tools
printf 'Image ready: %s/weather-station.img\n' "$BUILD"
printf 'Select "Use custom" in Raspberry Pi Imager. Skip OS customisation.\n'
