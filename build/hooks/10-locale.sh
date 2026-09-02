#!/bin/sh
# Locale française et fuseau Europe/Paris.
#
# UTF-8 est indispensable : la mémoire, les journaux et la console sont
# entièrement en français, et une locale C tronquerait les accents à
# l'affichage comme dans les fichiers de configuration.
set -eu
export DEBIAN_FRONTEND=noninteractive

apt-get install -y --no-install-recommends locales tzdata console-setup

sed -i 's/^# *\(fr_FR.UTF-8\)/\1/' /etc/locale.gen
sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen
locale-gen
update-locale LANG=fr_FR.UTF-8 LC_ALL=fr_FR.UTF-8

ln -sf /usr/share/zoneinfo/Europe/Paris /etc/localtime
echo "Europe/Paris" > /etc/timezone

cat > /etc/default/keyboard <<'EOL'
XKBMODEL="pc105"
XKBLAYOUT="fr"
XKBVARIANT="oss"
BACKSPACE="guess"
EOL
