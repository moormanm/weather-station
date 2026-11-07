#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" != 1 ] || [ "$1" = --help ]; then
    cat <<'EOF'
Usage: ./run-arm64-vm.sh path/to/weather-station.img
       ./run-arm64-vm.sh .image-build/arm64-vm-XXXXXXXX

Test the image's app and desktop in a generic ARM64 VM on an Apple Silicon Mac.
Requires QEMU (brew install qemu) and Docker with weather-station-image-tools.
The original image stays unchanged. An image path prepares a new VM-only image
with a Debian kernel and virtual graphics/network devices. Preparation needs
internet. A VM directory starts the saved VM without preparation.
The VM has four CPUs and 1 GiB RAM, but does not reproduce Pi 3B performance,
boot firmware, GPIO, or Wi-Fi hardware. Networking uses virtual Ethernet.
Close the QEMU window or press Ctrl+C to stop. VM files stay in .image-build/.
The local console.sock socket gives access to the serial console (login: pi/pi).
EOF
    exit 0
fi
for tool in qemu-system-aarch64 docker python3; do
    command -v "$tool" >/dev/null || { printf 'Install %s before running.\n' "$tool" >&2; exit 1; }
done
[ "$(uname -m)" = arm64 ] && [ "$(uname -s)" = Darwin ] || {
    printf 'This launcher requires an Apple Silicon Mac.\n' >&2
    exit 1
}
INPUT="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$1")"
if [ -d "$INPUT" ]; then
    VM="$INPUT"
    for file in disk.img kernel initrd; do
        [ -f "$VM/$file" ] || { printf 'Missing VM file: %s\n' "$VM/$file" >&2; exit 1; }
    done
else
    mkdir -p "$ROOT/.image-build"
    VM="$(mktemp -d "$ROOT/.image-build/arm64-vm-XXXXXXXX")"
    printf 'Preparing a VM-only copy. The installer image stays unchanged.\n'
    docker run --rm --privileged --entrypoint bash \
    --mount "type=bind,source=$INPUT,target=/original.img,readonly" \
    --mount "type=bind,source=$VM,target=/vm" \
    --mount "type=bind,source=$ROOT/image/prepare-arm64-vm.sh,target=/prepare.sh,readonly" \
    --mount type=volume,source=weather-station-vm-cache,target=/cache \
    --volume /work \
    --env "OUTPUT_UID=$(id -u)" --env "OUTPUT_GID=$(id -g)" \
    weather-station-image-tools /prepare.sh
fi
printf 'VM files and serial log: %s\n' "$VM"
exec qemu-system-aarch64 \
    -machine virt -accel hvf -cpu host -smp 4 -m 1G \
    -kernel "$VM/kernel" -initrd "$VM/initrd" \
    -append "console=ttyAMA0,115200 console=tty1 root=/dev/vda2 rootfstype=ext4 rootwait rw" \
    -drive "file=$VM/disk.img,format=raw,if=virtio" \
    -device virtio-gpu-pci,xres=1920,yres=1080 \
    -device qemu-xhci -device usb-kbd -device usb-tablet \
    -netdev user,id=network -device virtio-net-pci,netdev=network \
    -chardev "socket,id=console,path=$VM/console.sock,server=on,wait=off,logfile=$VM/serial.log" \
    -serial chardev:console \
    -qmp "unix:$VM/monitor.sock,server=on,wait=off" \
    -display cocoa
