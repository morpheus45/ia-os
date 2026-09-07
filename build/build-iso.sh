#!/usr/bin/env bash
# Construit une ISO agent-os amorçable, BIOS et UEFI.
#
# La base est Debian stable, pas un noyau écrit pour l'occasion : c'est ce
# qui garantit que le disque, la carte réseau et le contrôleur USB de la
# machine cible fonctionnent. Ce qui est sur mesure, c'est le userland —
# aucun bureau, aucun navigateur, un seul service au démarrage.
#
#   sudo ./build-iso.sh                     # image complète
#   SUITE=bookworm sudo ./build-iso.sh      # autre version de Debian
#   MIROIR=http://... sudo ./build-iso.sh   # miroir local

set -euo pipefail

ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RACINE_PROJET="$(cd "$ICI/.." && pwd)"

SUITE="${SUITE:-trixie}"
MIROIR="${MIROIR:-http://deb.debian.org/debian}"
ARCH="${ARCH:-amd64}"
TRAVAIL="${TRAVAIL:-$ICI/work}"
SORTIE="${SORTIE:-$ICI/out}"
CHROOT="$TRAVAIL/chroot"
ARBRE="$TRAVAIL/iso"
NOM_IMAGE="agent-os-${SUITE}-${ARCH}-$(date +%Y%m%d).iso"
ETIQUETTE="AGENT-OS"

info()   { printf '\033[36m::\033[0m %s\n' "$*"; }
succes() { printf '\033[32mok\033[0m %s\n' "$*"; }
erreur() { printf '\033[31méchec\033[0m %s\n' "$*" >&2; exit 1; }

# --- vérifications préalables ---------------------------------------------

[[ $EUID -eq 0 ]] || erreur "debootstrap et les montages exigent root"

manquants=()
for outil in debootstrap xorriso mksquashfs grub-mkstandalone; do
    command -v "$outil" >/dev/null 2>&1 || manquants+=("$outil")
done
if (( ${#manquants[@]} )); then
    erreur "outils manquants : ${manquants[*]}
Sur Debian ou Ubuntu :
  apt install debootstrap xorriso squashfs-tools grub-efi-amd64-bin grub-pc-bin mtools"
fi

# 12 Go : le chroot pèse environ 2,5 Go, le squashfs 900 Mo, et debootstrap
# a besoin de place pour ses archives avant de les nettoyer.
libre_ko=$(df --output=avail -k "$(dirname "$TRAVAIL")" | tail -1)
(( libre_ko > 12 * 1024 * 1024 )) || erreur \
    "espace insuffisant : $((libre_ko / 1024 / 1024)) Go libres, 12 Go nécessaires"

# --- démontage garanti ----------------------------------------------------
# Sans ce piège, une interruption laisse /proc et /dev montés dans le chroot.
# Un « rm -rf » sur un tel arbre efface alors des morceaux du système hôte.

demonter() {
    for point in dev/pts dev proc sys run; do
        mountpoint -q "$CHROOT/$point" && umount -lf "$CHROOT/$point" || true
    done
}
trap demonter EXIT INT TERM

# --- système de base ------------------------------------------------------

if [[ -d "$CHROOT/usr/bin" ]] && [[ -z "${REFAIRE:-}" ]]; then
    info "chroot existant réutilisé (REFAIRE=1 pour repartir de zéro)"
else
    info "suppression de l'ancien chroot"
    demonter
    rm -rf "$CHROOT"
    mkdir -p "$CHROOT"

    info "debootstrap $SUITE ($ARCH) — comptez dix à vingt minutes"
    debootstrap --arch="$ARCH" --variant=minbase \
        --include=systemd,dbus,apt-utils \
        "$SUITE" "$CHROOT" "$MIROIR"
fi

mkdir -p "$ARBRE"/{live,boot/grub,EFI/boot} "$SORTIE"

# --- préparation du chroot ------------------------------------------------

info "montage des systèmes virtuels"
mount -t proc  none  "$CHROOT/proc"
mount -t sysfs none  "$CHROOT/sys"
mount -o bind  /dev  "$CHROOT/dev"
mount -t devpts none "$CHROOT/dev/pts"

cat > "$CHROOT/etc/apt/sources.list" <<EOF
deb $MIROIR $SUITE main contrib non-free-firmware
deb $MIROIR ${SUITE}-updates main contrib non-free-firmware
deb http://security.debian.org/debian-security ${SUITE}-security main contrib non-free-firmware
EOF

# Empêche les paquets de démarrer leurs services pendant la construction :
# dans un chroot, systemd n'écoute pas et l'installation échouerait.
cat > "$CHROOT/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod +x "$CHROOT/usr/sbin/policy-rc.d"

echo "agent-os" > "$CHROOT/etc/hostname"
cat > "$CHROOT/etc/hosts" <<'EOF'
127.0.0.1   localhost agent-os
::1         localhost ip6-localhost ip6-loopback
EOF

# --- paquets --------------------------------------------------------------

paquets="$(grep -vE '^\s*(#|$)' "$ICI/config/packages.list" | tr '\n' ' ')"
info "installation des paquets"
chroot "$CHROOT" /bin/bash -eux <<EOF
export DEBIAN_FRONTEND=noninteractive LC_ALL=C LANG=C
apt-get update
apt-get install -y --no-install-recommends $paquets
EOF

# --- hooks ----------------------------------------------------------------

for hook in "$ICI"/hooks/*.sh; do
    [[ -f "$hook" ]] || continue
    info "hook $(basename "$hook")"
    cp "$hook" "$CHROOT/tmp/hook.sh"
    chmod +x "$CHROOT/tmp/hook.sh"
    chroot "$CHROOT" /tmp/hook.sh
    rm -f "$CHROOT/tmp/hook.sh"
done

# --- agent-os -------------------------------------------------------------

info "installation d'agent-os dans l'image"
rm -rf "$CHROOT/tmp/agent-os"
mkdir -p "$CHROOT/tmp/agent-os"
cp -r "$RACINE_PROJET"/{runtime,system,db,install,docs,README.md} \
      "$CHROOT/tmp/agent-os/" 2>/dev/null || true
chroot "$CHROOT" /bin/bash -c "RACINE=/ /tmp/agent-os/system/install-system.sh"

# L'installateur doit être à portée de main dès l'ouverture de session.
install -D -m 0755 "$RACINE_PROJET/install/install.sh" \
    "$CHROOT/usr/local/bin/agentos-installer"

# --- session live ---------------------------------------------------------

info "configuration de la session live"
chroot "$CHROOT" /bin/bash -eux <<'EOF'
export DEBIAN_FRONTEND=noninteractive
# Compte live sans mot de passe : l'image sert à installer, pas à travailler.
# L'installateur pose un vrai mot de passe sur le système installé.
useradd -m -s /bin/bash -G sudo live 2>/dev/null || true
passwd -d live
passwd -l root

# Le compte live n'a pas de mot de passe : sudo en demanderait un que
# personne ne pourrait fournir. Sur une image d'installation, dont c'est le
# seul rôle, l'élévation sans mot de passe est le comportement attendu — et
# l'installateur supprime ce fichier sur le système installé.
cat > /etc/sudoers.d/live <<'EOL'
live ALL=(ALL) NOPASSWD:ALL
EOL
chmod 0440 /etc/sudoers.d/live

# La console d'installation s'ouvre sans demander d'identifiants : sur une
# image live, un mot de passe n'ajoute rien et bloque une machine sans clavier.
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<'EOL'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin live --noclear %I $TERM
EOL

# En live, le runtime ne démarre pas : rien à mémoriser sur un disque en RAM.
systemctl disable agentos.service agentos-model.service agentos-backup.timer || true
systemctl enable ssh || true

cat > /home/live/.bash_profile <<'EOL'
cat <<'BANNIERE'

  agent-os — image d'installation

  Installer sur le disque :   sudo agentos-installer
  Documentation :             /usr/share/doc/agent-os/
  Vérifier le matériel :      lsblk ; ip addr ; free -h

BANNIERE
EOL
chown live:live /home/live/.bash_profile
EOF

# --- allègement -----------------------------------------------------------

info "nettoyage de l'image"
chroot "$CHROOT" /bin/bash -eux <<'EOF'
export DEBIAN_FRONTEND=noninteractive
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/locale/*
# Les clés d'hôte SSH doivent être uniques par machine : les laisser dans
# l'image donnerait la même identité à toutes les installations, ce qui
# permet de se faire passer pour l'une d'elles.
rm -f /etc/ssh/ssh_host_*
rm -f /etc/machine-id /var/lib/dbus/machine-id
# Un machine-id vide (et non absent) fait régénérer l'identifiant au premier
# démarrage, ce que systemd attend explicitement.
touch /etc/machine-id
rm -rf /tmp/* /var/tmp/* /root/.bash_history
find /var/log -type f -delete
EOF
rm -f "$CHROOT/usr/sbin/policy-rc.d"

# --- extraction du noyau --------------------------------------------------

info "extraction du noyau et de l'initrd"
noyau="$(ls -1 "$CHROOT"/boot/vmlinuz-* 2>/dev/null | sort -V | tail -1)" \
    || erreur "aucun noyau dans l'image"
initrd="$(ls -1 "$CHROOT"/boot/initrd.img-* 2>/dev/null | sort -V | tail -1)" \
    || erreur "aucun initrd dans l'image"
cp "$noyau"  "$ARBRE/live/vmlinuz"
cp "$initrd" "$ARBRE/live/initrd.img"
version_noyau="$(basename "$noyau" | sed 's/vmlinuz-//')"

demonter

# --- squashfs -------------------------------------------------------------

info "compression du système de fichiers"
rm -f "$ARBRE/live/filesystem.squashfs"
# zstd plutôt que xz : deux fois plus rapide à décompresser au démarrage,
# pour une image à peine plus grosse. Sur une machine qui démarre depuis une
# clé USB, c'est la décompression qui domine le temps de boot.
mksquashfs "$CHROOT" "$ARBRE/live/filesystem.squashfs" \
    -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet \
    -e boot/vmlinuz-* boot/initrd.img-*

# --- amorçage -------------------------------------------------------------

info "configuration de GRUB"
cp "$ICI/grub/grub.cfg" "$ARBRE/boot/grub/grub.cfg"

# La police Unicode est posée sur l'image, pas embarquée dans les binaires
# d'amorçage : l'image BIOS est plafonnée à 480 Ko et la police en fait
# 2,4 Mo — l'embarquer fait échouer grub-mkstandalone. GRUB la charge après
# avoir trouvé le support. Sans elle, les accents du menu s'affichent en
# points d'interrogation.
mkdir -p "$ARBRE/boot/grub/fonts"
for source in /usr/share/grub/unicode.pf2 /usr/share/grub2/unicode.pf2; do
    [[ -f "$source" ]] && cp "$source" "$ARBRE/boot/grub/fonts/unicode.pf2" && break
done
[[ -f "$ARBRE/boot/grub/fonts/unicode.pf2" ]] \
    || avert "police unicode introuvable : les accents du menu seront illisibles"

# Image EFI autonome : elle embarque GRUB et sa configuration, si bien que la
# clé démarre sur une machine UEFI sans rien installer sur l'ESP.
#
# La police Unicode est embarquée : sans elle, GRUB rend les accents en
# points d'interrogation. Le surcoût est d'environ deux mégaoctets.
grub-mkstandalone \
    --format=x86_64-efi \
    --output="$ARBRE/EFI/boot/bootx64.efi" \
    --locales="" --fonts="" \
    --modules="part_gpt part_msdos iso9660 search search_label all_video gfxterm font" \
    "boot/grub/grub.cfg=$ARBRE/boot/grub/grub.cfg"

# Partition EFI en FAT, embarquée dans l'ISO : c'est elle que le micrologiciel
# monte pour trouver bootx64.efi.
efi_img="$TRAVAIL/efiboot.img"
rm -f "$efi_img"
truncate -s 12M "$efi_img"
mkfs.vfat -n AGENTOS-EFI "$efi_img" >/dev/null
mmd  -i "$efi_img" ::/EFI ::/EFI/boot
mcopy -i "$efi_img" "$ARBRE/EFI/boot/bootx64.efi" ::/EFI/boot/
cp "$efi_img" "$ARBRE/boot/grub/efiboot.img"

# Image BIOS : El Torito classique, pour les machines sans UEFI.
grub-mkstandalone \
    --format=i386-pc \
    --output="$TRAVAIL/core.img" \
    --install-modules="linux normal iso9660 biosdisk memdisk search search_label \
        tar ls part_gpt part_msdos all_video gfxterm font" \
    --modules="linux normal iso9660 biosdisk search search_label" \
    --locales="" --fonts="" \
    "boot/grub/grub.cfg=$ARBRE/boot/grub/grub.cfg"
cat /usr/lib/grub/i386-pc/cdboot.img "$TRAVAIL/core.img" > "$ARBRE/boot/grub/bios.img"

echo "$version_noyau" > "$ARBRE/live/noyau.txt"

# --- assemblage -----------------------------------------------------------

info "assemblage de l'ISO"
xorriso -as mkisofs \
    -iso-level 3 \
    -volid "$ETIQUETTE" \
    -full-iso9660-filenames \
    -joliet -rational-rock \
    -eltorito-boot boot/grub/bios.img \
        -no-emul-boot -boot-load-size 4 -boot-info-table \
        --eltorito-catalog boot/grub/boot.cat \
    --grub2-boot-info \
    --grub2-mbr /usr/lib/grub/i386-pc/boot_hybrid.img \
    -eltorito-alt-boot \
        -e boot/grub/efiboot.img \
        -no-emul-boot \
        -isohybrid-gpt-basdat \
    -output "$SORTIE/$NOM_IMAGE" \
    "$ARBRE"

cd "$SORTIE"
sha256sum "$NOM_IMAGE" > "$NOM_IMAGE.sha256"

succes "image prête : $SORTIE/$NOM_IMAGE ($(du -h "$NOM_IMAGE" | cut -f1))"
cat <<EOF

Écrire sur une clé USB (vérifier le nom du périphérique — cette commande
efface irrémédiablement son contenu) :

    lsblk
    sudo dd if=$SORTIE/$NOM_IMAGE of=/dev/sdX bs=4M status=progress conv=fsync

Essayer sans matériel :

    qemu-system-x86_64 -m 4096 -cdrom $SORTIE/$NOM_IMAGE

Une fois démarré : sudo agentos-installer
EOF
