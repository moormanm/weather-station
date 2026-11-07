#!/bin/bash
set -euo pipefail

HOME_DIR="/home/${FIRST_USER_NAME}"
SHARE="${ROOTFS_DIR}/usr/local/share/weather-station"
install -d "$SHARE" "${ROOTFS_DIR}/usr/local/bin" \
    "${ROOTFS_DIR}/usr/share/xsessions" "${ROOTFS_DIR}/etc/lightdm/lightdm.conf.d" \
    "${ROOTFS_DIR}${HOME_DIR}/Desktop" \
    "${ROOTFS_DIR}${HOME_DIR}/.config/pcmanfm/weather-station"
if [ ! files/weather-station -ef "${ROOTFS_DIR}${HOME_DIR}/weather-station" ]; then
    cp -a files/weather-station "${ROOTFS_DIR}${HOME_DIR}/weather-station"
fi
if [ ! files/ws-settings.ini -ef "${ROOTFS_DIR}${HOME_DIR}/ws-settings.ini" ]; then
    install -m 600 files/ws-settings.ini "${ROOTFS_DIR}${HOME_DIR}/ws-settings.ini"
fi
install -m 644 files/wallpaper "$SHARE/wallpaper"
install -m 644 files/openbox.xml "$SHARE/openbox.xml"
install -m 755 files/weather-station-session files/weather-station-launch \
    files/weather-station-settings \
    "${ROOTFS_DIR}/usr/local/bin/"

cat > "${ROOTFS_DIR}/usr/share/xsessions/weather-station.desktop" <<'EOF'
[Desktop Entry]
Name=Weather Station
Type=Application
Exec=/usr/local/bin/weather-station-session
EOF

cat > "${ROOTFS_DIR}/etc/lightdm/lightdm.conf.d/90-weather-station.conf" <<EOF
[Seat:*]
autologin-user=${FIRST_USER_NAME}
autologin-user-timeout=0
autologin-session=weather-station
user-session=weather-station
xserver-command=X -s 0 -dpms
EOF

cat > "${ROOTFS_DIR}${HOME_DIR}/Desktop/weather-station.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Start Weather Station
Exec=/usr/local/bin/weather-station-launch
Icon=weather-clear
Terminal=false
EOF

cat > "${ROOTFS_DIR}${HOME_DIR}/Desktop/wifi.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Wi-Fi Settings
Exec=xterm -T "Wi-Fi Settings" -e nmtui
Icon=network-wireless
Terminal=false
EOF

cat > "${ROOTFS_DIR}${HOME_DIR}/Desktop/settings.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Edit Weather Station Settings
Exec=/usr/local/bin/weather-station-settings
Icon=accessories-text-editor
Terminal=false
EOF
chmod 755 "${ROOTFS_DIR}${HOME_DIR}/Desktop/"*.desktop

cat > "${ROOTFS_DIR}${HOME_DIR}/.config/pcmanfm/weather-station/pcmanfm.conf" <<'EOF'
[config]
bm_open_method=0

[ui]
always_show_tabs=0

[desktop]
show_wm_menu=1
EOF

cat > "${ROOTFS_DIR}${HOME_DIR}/.config/pcmanfm/weather-station/desktop-items-0.conf" <<'EOF'
[*]
wallpaper_mode=crop
wallpaper_common=1
wallpaper=/usr/local/share/weather-station/wallpaper
desktop_bg=#0a0e1a
desktop_fg=#ffffff
desktop_shadow=#000000
desktop_font=DejaVu Sans 12
show_wm_menu=1
show_documents=0
show_trash=0
show_mounts=0
show_home=0
EOF

on_chroot <<EOF
chown -R ${FIRST_USER_NAME}:${FIRST_USER_NAME} ${HOME_DIR}/weather-station \
    ${HOME_DIR}/ws-settings.ini ${HOME_DIR}/Desktop ${HOME_DIR}/.config/pcmanfm/weather-station
systemctl enable lightdm NetworkManager
systemctl set-default graphical.target
EOF
