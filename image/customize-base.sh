#!/usr/bin/env bash
set -euo pipefail

mkdir -p /cache/apt/archives /cache/apt/lists/partial /work/root
exec 9>/cache/build.lock
printf 'Waiting for the image cache lock.\n'
flock 9
ROOTFS_DIR=/work/root
LOOP=
REGISTERED=0
cleanup() {
    local failed=0
    for mount in \
        "$ROOTFS_DIR/var/cache/apt/archives" "$ROOTFS_DIR/var/lib/apt/lists" \
        "$ROOTFS_DIR/dev/pts" "$ROOTFS_DIR/dev" "$ROOTFS_DIR/proc" \
        "$ROOTFS_DIR/sys" "$ROOTFS_DIR/run" "$ROOTFS_DIR/boot/firmware" "$ROOTFS_DIR"; do
        if mountpoint -q "$mount"; then
            umount "$mount" || failed=1
        fi
    done
    if [ "$REGISTERED" = 1 ]; then
        echo -1 > /proc/sys/fs/binfmt_misc/weather-station-aarch64 || failed=1
    fi
    if [ "$failed" = 0 ] && [ -n "$LOOP" ]; then
        losetup -d "$LOOP" || failed=1
        LOOP=
    fi
    return "$failed"
}
trap cleanup EXIT

if ! arch-test -c /work/root arm64; then
    if ! mountpoint -q /proc/sys/fs/binfmt_misc; then
        mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
    fi
    [ ! -e /proc/sys/fs/binfmt_misc/weather-station-aarch64 ] || {
        echo 'Another build uses the ARM emulator registration.' >&2
        exit 1
    }
    echo ':weather-station-aarch64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-aarch64:F' > /proc/sys/fs/binfmt_misc/register
    REGISTERED=1
    arch-test -c /work/root arm64
fi

BASE="/cache/$BASE_SHA.img"
if [ ! -f "$BASE" ]; then
    printf 'Extracting the official image into the cache.\n'
    xz -dc /base.img.xz > "$BASE.partial"
    mv "$BASE.partial" "$BASE"
fi
printf 'Copying the cached base and adding 3 GiB for the desktop and app.\n'
cp --reflink=auto --sparse=always "$BASE" /work/weather-station.img
truncate -s +3G /work/weather-station.img
LOOP="$(losetup --find --show --partscan /work/weather-station.img)"
parted -s "$LOOP" resizepart 2 100%
partx -u "$LOOP"
for partition in 1 2; do
    node="${LOOP}p${partition}"
    if [ ! -b "$node" ]; then
        major_minor="$(lsblk -nr -o MAJ:MIN "$LOOP" | sed -n "$((partition + 1))p")"
        mknod "$node" b "${major_minor%:*}" "${major_minor#*:}"
    fi
done
status=0
e2fsck -pf "${LOOP}p2" || status=$?
[ "$status" -le 1 ]
resize2fs "${LOOP}p2"
mount "${LOOP}p2" "$ROOTFS_DIR"
mount "${LOOP}p1" "$ROOTFS_DIR/boot/firmware"
mount --bind /dev "$ROOTFS_DIR/dev"
mount --bind /dev/pts "$ROOTFS_DIR/dev/pts"
mount -t proc proc "$ROOTFS_DIR/proc"
mount -t sysfs sysfs "$ROOTFS_DIR/sys"
mount -t tmpfs tmpfs "$ROOTFS_DIR/run"
mount --bind /cache/apt/archives "$ROOTFS_DIR/var/cache/apt/archives"
mount --bind /cache/apt/lists "$ROOTFS_DIR/var/lib/apt/lists"

# Do not start services or use the image's first-boot DNS link during the build.
cp -a "$ROOTFS_DIR/etc/resolv.conf" /work/resolv.conf
rm "$ROOTFS_DIR/etc/resolv.conf"
cp /etc/resolv.conf "$ROOTFS_DIR/etc/resolv.conf"
if [ -e "$ROOTFS_DIR/usr/sbin/policy-rc.d" ]; then
    cp -a "$ROOTFS_DIR/usr/sbin/policy-rc.d" /work/policy-rc.d
fi
printf '#!/bin/sh\nexit 101\n' > "$ROOTFS_DIR/usr/sbin/policy-rc.d"
chmod 755 "$ROOTFS_DIR/usr/sbin/policy-rc.d"
on_chroot() {
    chroot "$ROOTFS_DIR" /bin/bash -e "$@"
}
export ROOTFS_DIR
export -f on_chroot
on_chroot <<'EOF'
export DEBIAN_FRONTEND=noninteractive
if ! id pi >/dev/null 2>&1; then
    useradd -m -s /bin/bash pi
fi
usermod -s /bin/bash pi
printf 'pi:pi\n' | chpasswd
for group in adm dialout cdrom audio video plugdev users sudo input render netdev gpio spi i2c; do
    if getent group "$group" >/dev/null; then
        usermod -aG "$group" pi
    fi
done
systemctl disable ssh
touch /etc/cloud/cloud-init.disabled
systemctl mask userconfig.service
EOF
printf 'Installing the small desktop and app packages (downloads are cached).\n'
on_chroot <<'EOF'
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
EOF
PACKAGES="$(tr '\n' ' ' < /tools/00-packages)"
on_chroot -c "export DEBIAN_FRONTEND=noninteractive; apt-get -y --no-install-recommends \
    -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 \
    -o APT::Keep-Downloaded-Packages=true install $PACKAGES"

cd /output
FIRST_USER_NAME=pi bash /tools/00-run.sh
on_chroot <<'EOF'
SUDO_USER=pi raspi-config nonint do_wifi_country US
systemctl disable apt-listchanges.timer 2>/dev/null || true
python3 -c 'import pygame'
EOF
rm "$ROOTFS_DIR/etc/resolv.conf"
cp -a /work/resolv.conf "$ROOTFS_DIR/etc/resolv.conf"
if [ -e /work/policy-rc.d ]; then
    cp -a /work/policy-rc.d "$ROOTFS_DIR/usr/sbin/policy-rc.d"
else
    rm "$ROOTFS_DIR/usr/sbin/policy-rc.d"
fi
sync
cleanup
trap - EXIT
printf 'Saving the image without compression.\n'
cp --sparse=always /work/weather-station.img /output/weather-station.img.partial
chown "$OUTPUT_UID:$OUTPUT_GID" /output/weather-station.img.partial
chmod 600 /output/weather-station.img.partial
mv /output/weather-station.img.partial /output/weather-station.img
