#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" != 1 ] || [ "$1" = --help ]; then
    cat <<'EOF'
Usage: ./run-pi3-vm.sh path/to/weather-station.img

Run a Raspberry Pi 3B model with four Cortex-A53 cores and 1 GiB RAM.
Requires QEMU (brew install qemu) and Docker with weather-station-image-tools.
Requires fdtput (brew install dtc).
The original image stays unchanged. Each run creates a new writable overlay.
QEMU directly loads the image's kernel and device tree, not the Pi boot firmware.
Experimental: the current image reaches systemd but SD I/O timeouts prevent
desktop startup under QEMU 11.1.3. It is not yet usable for app testing.
This is not a hardware acceptance test. Wi-Fi and VideoCore GPU acceleration
are not reproduced. Emulation is slower than a physical Pi.
Close the QEMU window or press Ctrl+C to stop. Serial logs are saved with the VM.
EOF
    exit 0
fi
for tool in qemu-system-aarch64 qemu-img fdtput docker python3; do
    command -v "$tool" >/dev/null || { printf 'Install %s before running.\n' "$tool" >&2; exit 1; }
done
IMAGE="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$1")"
mkdir -p "$ROOT/.image-build"
VM="$(mktemp -d "$ROOT/.image-build/pi3-vm-XXXXXXXX")"
docker run --rm --privileged --entrypoint bash \
    --mount "type=bind,source=$IMAGE,target=/disk.img,readonly" \
    --mount "type=bind,source=$VM,target=/vm" \
    --env "OUTPUT_UID=$(id -u)" --env "OUTPUT_GID=$(id -g)" \
    weather-station-image-tools -ec '
        mkdir /boot-image
        loop=$(losetup --find --show --partscan --read-only /disk.img)
        cleanup() {
            if mountpoint -q /boot-image; then umount /boot-image; fi
            losetup -d "$loop"
        }
        trap cleanup EXIT
        node="${loop}p1"
        if [ ! -b "$node" ]; then
            device=$(lsblk -nr -o MAJ:MIN "$loop" | sed -n "2p")
            mknod "$node" b "${device%:*}" "${device#*:}"
        fi
        mount -o ro "$node" /boot-image
        cp /boot-image/kernel8.img /boot-image/bcm2710-rpi-3-b.dtb \
            /boot-image/initramfs8 /vm/
        chown "$OUTPUT_UID:$OUTPUT_GID" /vm/*
    '
python3 - "$VM/kernel8.img" "$VM/kernel" <<'PY'
import gzip
import sys
from pathlib import Path

data = Path(sys.argv[1]).read_bytes()
Path(sys.argv[2]).write_bytes(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
PY
# QEMU needs SD-host PIO and non-FIQ USB interrupts. Only VM files change.
fdtput -d "$VM/bcm2710-rpi-3-b.dtb" /soc/mmc@7e202000 dmas dma-names
qemu-img create -f qcow2 -F raw -b "$IMAGE" "$VM/sd.qcow2"
qemu-img resize "$VM/sd.qcow2" 8G
printf 'VM files and serial log: %s\n' "$VM"
exec qemu-system-aarch64 \
    -machine raspi3b -accel tcg -smp 4 -m 1G \
    -kernel "$VM/kernel" -dtb "$VM/bcm2710-rpi-3-b.dtb" \
    -initrd "$VM/initramfs8" \
    -append "earlycon=pl011,mmio32,0x3f201000 keep_bootcon console=ttyAMA0,115200 console=tty1 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait rw dwc_otg.fiq_fsm_enable=0 dwc_otg.fiq_enable=0 watchdog_disarm=0" \
    -drive "file=$VM/sd.qcow2,format=qcow2,if=sd" \
    -device usb-kbd -device usb-mouse \
    -serial "file:$VM/serial.log" \
    -qmp "unix:$VM/monitor.sock,server=on,wait=off" \
    -display cocoa
