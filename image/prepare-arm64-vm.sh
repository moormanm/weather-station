#!/usr/bin/env bash
set -euo pipefail

mkdir -p /work/root /cache/archives
exec 9>/cache/build.lock
flock 9
ROOT=/work/root
LOOP=
cleanup() {
    local failed=0
    for mount in "$ROOT/var/cache/apt/archives" "$ROOT/dev/pts" "$ROOT/dev" \
        "$ROOT/proc" "$ROOT/sys" "$ROOT/run" "$ROOT/boot/firmware" "$ROOT"; do
        if mountpoint -q "$mount"; then umount "$mount" || failed=1; fi
    done
    if [ "$failed" = 0 ] && [ -n "$LOOP" ]; then
        losetup -d "$LOOP"
        LOOP=
    fi
    return "$failed"
}
trap cleanup EXIT
cp --reflink=auto --sparse=always /original.img /work/disk.img
LOOP="$(losetup --find --show --partscan /work/disk.img)"
for partition in 1 2; do
    node="${LOOP}p${partition}"
    if [ ! -b "$node" ]; then
        device="$(lsblk -nr -o MAJ:MIN "$LOOP" | sed -n "$((partition + 1))p")"
        mknod "$node" b "${device%:*}" "${device#*:}"
    fi
done
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sysfs "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"
mount --bind /cache/archives "$ROOT/var/cache/apt/archives"
cp -a "$ROOT/etc/resolv.conf" /work/resolv.conf
rm "$ROOT/etc/resolv.conf"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
if [ -e "$ROOT/usr/sbin/policy-rc.d" ] || [ -L "$ROOT/usr/sbin/policy-rc.d" ]; then
    mv "$ROOT/usr/sbin/policy-rc.d" /work/policy-rc.d
fi
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
chmod 755 "$ROOT/usr/sbin/policy-rc.d"
chroot "$ROOT" /bin/bash -e <<'EOF'
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
apt-get -y --no-install-recommends -o APT::Keep-Downloaded-Packages=true \
    -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install linux-image-arm64
EOF
KERNEL="$(find "$ROOT/boot" -maxdepth 1 -name 'vmlinuz-*-arm64' | sort -V | tail -n 1)"
[ -n "$KERNEL" ]
VERSION="${KERNEL##*/vmlinuz-}"
printf '\nvirtio_pci\nvirtio_blk\nvirtio_gpu\nvirtio_net\n' >> "$ROOT/etc/initramfs-tools/modules"
chroot "$ROOT" update-initramfs -u -k "$VERSION"
cp "$KERNEL" /vm/kernel
cp "$ROOT/boot/initrd.img-$VERSION" /vm/initrd
rm "$ROOT/usr/sbin/policy-rc.d" "$ROOT/etc/resolv.conf"
if [ -e /work/policy-rc.d ] || [ -L /work/policy-rc.d ]; then
    mv /work/policy-rc.d "$ROOT/usr/sbin/policy-rc.d"
fi
cp -a /work/resolv.conf "$ROOT/etc/resolv.conf"
sync
cleanup
trap - EXIT
cp --sparse=always /work/disk.img /vm/disk.img
chown "$OUTPUT_UID:$OUTPUT_GID" /vm/disk.img /vm/kernel /vm/initrd
