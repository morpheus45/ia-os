#!/usr/bin/env bash
# Installe agent-os sur un disque.
#
# CE SCRIPT EFFACE ENTIÈREMENT LE DISQUE CHOISI.
#
# Le partitionnement est calculé à partir de la taille réelle du disque et
# de la mémoire vive. Un disque vendu « 500 Go » offre 465 Gio : les tailles
# annoncées par les constructeurs comptent en puissances de dix, le noyau en
# puissances de deux. Un plan fixe additionnant des « Go » commerciaux
# déborderait de dix pour cent.

set -euo pipefail

CIBLE=""
SIMULATION=0
CHIFFRER=0
NOM_MACHINE="agent-os"

# --- tailles fixes, en Mio ------------------------------------------------
TAILLE_EFI=512
TAILLE_RACINE=$((40 * 1024))
# Part de l'espace restant réservée à la mémoire de l'agent. Le reste va aux
# modèles, qui sont volumineux (4 à 30 Gio pièce) mais remplaçables : ils se
# retéléchargent, la mémoire non.
PART_MEMOIRE=20
MINIMUM_DISQUE=$((120 * 1024))
# Réserve de fin de disque : la table GPT secondaire, plus la marge que
# consomme l'alignement de chaque partition sur 1 Mio. Deux Mio suffisent en
# théorie ; seize évitent que l'arrondi de la dernière partition ne fasse
# échouer sgdisk sur un disque dont la taille n'est pas un multiple rond.
RESERVE=16

info()   { printf '\033[36m::\033[0m %s\n' "$*"; }
succes() { printf '\033[32mok\033[0m %s\n' "$*"; }
avert()  { printf '\033[33m! \033[0m %s\n' "$*"; }
erreur() { printf '\033[31méchec\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage : agentos-installer [options]

  -d, --disque /dev/sdX   disque cible (sans cette option, la liste est proposée)
  -n, --nom NOM           nom de la machine (défaut : agent-os)
      --chiffrer          chiffre la partition de mémoire (LUKS2)
      --simulation        affiche le plan sans rien écrire
  -h, --aide              cette aide

Le chiffrement protège la mémoire si la machine est volée. Il exige en
contrepartie une phrase de passe à chaque démarrage : une machine censée
redémarrer seule après une coupure de courant ne repartira pas sans
quelqu'un pour la saisir. À n'activer que si la machine est physiquement
exposée et qu'une présence au redémarrage est acceptable.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--disque)     CIBLE="${2:-}"; shift 2 ;;
        -n|--nom)        NOM_MACHINE="${2:-}"; shift 2 ;;
        --chiffrer)      CHIFFRER=1; shift ;;
        --simulation)    SIMULATION=1; shift ;;
        -h|--aide)       usage; exit 0 ;;
        *) erreur "option inconnue : $1 (voir --aide)" ;;
    esac
done

[[ $EUID -eq 0 ]] || erreur "l'installation exige root (sudo agentos-installer)"

for outil in sgdisk mkfs.ext4 mkfs.vfat mkswap blkid lsblk rsync; do
    command -v "$outil" >/dev/null 2>&1 || erreur "outil manquant : $outil"
done

# --- choix du disque ------------------------------------------------------

lister_disques() {
    lsblk -dpno NAME,SIZE,MODEL,TYPE | awk '$4=="disk"{ $4=""; print }'
}

if [[ -z "$CIBLE" ]]; then
    echo "Disques détectés :"
    echo
    lister_disques | nl -w3 -s') '
    echo
    read -rp "Chemin du disque à effacer (ex. /dev/sda) : " CIBLE
fi

[[ -b "$CIBLE" ]] || erreur "« $CIBLE » n'est pas un périphérique bloc"

# Refuser un disque en cours d'utilisation. Sans ce contrôle, installer sur
# la clé USB depuis laquelle on a démarré détruit le système en cours
# d'exécution — et l'erreur ne se voit qu'une fois le partitionnement fait.
if findmnt -no SOURCE / | grep -q "^$CIBLE"; then
    erreur "$CIBLE porte le système en cours d'exécution"
fi
montages="$(lsblk -no MOUNTPOINT "$CIBLE" | grep -v '^$' | grep -v '^/run/live' || true)"
if [[ -n "$montages" ]]; then
    avert "partitions montées sur $CIBLE :"
    echo "$montages" | sed 's/^/    /'
    erreur "démonter d'abord, ou choisir un autre disque"
fi

# --- calcul du plan -------------------------------------------------------

octets_disque="$(blockdev --getsize64 "$CIBLE")"
mio_disque=$((octets_disque / 1024 / 1024))
mio_ram=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))

(( mio_disque >= MINIMUM_DISQUE )) || erreur \
    "disque trop petit : $((mio_disque / 1024)) Gio, minimum $((MINIMUM_DISQUE / 1024)) Gio"

# Swap égal à la RAM, plafonné à 16 Gio : c'est ce qu'exige l'hibernation, et
# au-delà l'espace est mieux employé à stocker des modèles.
mio_swap=$(( mio_ram < 16384 ? mio_ram : 16384 ))

reste=$(( mio_disque - TAILLE_EFI - TAILLE_RACINE - mio_swap - RESERVE ))
(( reste > 20480 )) || erreur "espace insuffisant après les partitions fixes"

mio_memoire=$(( reste * PART_MEMOIRE / 100 ))
mio_modeles=$(( reste - mio_memoire ))

# `bc` n'est pas garanti sur une image minimale ; awk l'est.
go() { awk -v m="$1" 'BEGIN{ printf "%.1f Gio", m/1024 }'; }

cat <<EOF

  Disque      : $CIBLE  ($(go $mio_disque))
  Mémoire vive: $(go $mio_ram)
  Machine     : $NOM_MACHINE
  Chiffrement : $([[ $CHIFFRER -eq 1 ]] && echo "oui (LUKS2 sur la mémoire)" || echo non)

  Partitionnement prévu :

    ${CIBLE}1   $(go $TAILLE_EFI)   FAT32   /boot/efi          amorçage
    ${CIBLE}2   $(go $mio_swap)   swap    —                  hibernation
    ${CIBLE}3   $(go $TAILLE_RACINE)   ext4    /                  système
    ${CIBLE}4   $(go $mio_memoire)   ext4    /var/lib/agentos   mémoire de l'agent
    ${CIBLE}5   $(go $mio_modeles)   ext4    /var/lib/models    modèles locaux

EOF

if (( SIMULATION )); then
    succes "simulation : rien n'a été écrit"
    exit 0
fi

echo "TOUT LE CONTENU DE $CIBLE SERA DÉFINITIVEMENT EFFACÉ."
read -rp 'Taper « EFFACER » en majuscules pour confirmer : ' confirmation
[[ "$confirmation" == "EFFACER" ]] || { echo "Annulé."; exit 1; }

# --- partitionnement ------------------------------------------------------

info "écriture de la table de partitions"
# `wipefs` d'abord : sans cela, une ancienne signature de RAID ou de LVM
# survit au partitionnement et le noyau réactive le volume au démarrage
# suivant, rendant les nouvelles partitions inaccessibles.
wipefs --all --force "$CIBLE" >/dev/null
sgdisk --zap-all "$CIBLE" >/dev/null

sgdisk \
    --new=1:1M:+${TAILLE_EFI}M   --typecode=1:ef00 --change-name=1:"EFI" \
    --new=2:0:+${mio_swap}M      --typecode=2:8200 --change-name=2:"swap" \
    --new=3:0:+${TAILLE_RACINE}M --typecode=3:8304 --change-name=3:"racine" \
    --new=4:0:+${mio_memoire}M   --typecode=4:8300 --change-name=4:"memoire" \
    --new=5:0:0                  --typecode=5:8300 --change-name=5:"modeles" \
    "$CIBLE" >/dev/null

partprobe "$CIBLE" 2>/dev/null || true
udevadm settle --timeout=30 2>/dev/null || sleep 3

# Les disques NVMe et mmc intercalent un « p » avant le numéro de partition.
partition() {
    case "$CIBLE" in
        *nvme*|*mmcblk*) echo "${CIBLE}p$1" ;;
        *)               echo "${CIBLE}$1" ;;
    esac
}
P_EFI="$(partition 1)"; P_SWAP="$(partition 2)"; P_RACINE="$(partition 3)"
P_MEMOIRE="$(partition 4)"; P_MODELES="$(partition 5)"

for p in "$P_EFI" "$P_SWAP" "$P_RACINE" "$P_MEMOIRE" "$P_MODELES"; do
    [[ -b "$p" ]] || erreur "partition attendue absente : $p"
done

# --- chiffrement ----------------------------------------------------------

MEMOIRE_BRUTE="$P_MEMOIRE"
if (( CHIFFRER )); then
    info "chiffrement de la partition de mémoire"
    cryptsetup luksFormat --type luks2 --batch-mode "$P_MEMOIRE" \
        || erreur "chiffrement impossible"
    cryptsetup open "$P_MEMOIRE" agentos-memoire
    MEMOIRE_BRUTE="/dev/mapper/agentos-memoire"
fi

# --- systèmes de fichiers -------------------------------------------------

info "formatage"
mkfs.vfat -F32 -n EFI "$P_EFI" >/dev/null
mkswap -L swap "$P_SWAP" >/dev/null
mkfs.ext4 -q -L racine "$P_RACINE"
# La mémoire est faite de très nombreux petits enregistrements : un inode
# tous les 8 Kio au lieu de 16 évite d'épuiser la table avant l'espace.
mkfs.ext4 -q -L memoire -i 8192 "$MEMOIRE_BRUTE"
# Les modèles sont quelques fichiers énormes : l'inverse exactement.
mkfs.ext4 -q -L modeles -i 1048576 "$P_MODELES"

# --- copie du système -----------------------------------------------------

CIBLE_MNT="/mnt/agentos"
info "montage de la cible"
mkdir -p "$CIBLE_MNT"
mount "$P_RACINE" "$CIBLE_MNT"
mkdir -p "$CIBLE_MNT"/{boot/efi,var/lib/agentos,var/lib/models}
mount "$P_EFI" "$CIBLE_MNT/boot/efi"
mount "$MEMOIRE_BRUTE" "$CIBLE_MNT/var/lib/agentos"
mount "$P_MODELES" "$CIBLE_MNT/var/lib/models"

info "copie du système (quelques minutes)"
SOURCE_LIVE="/run/live/medium"
if [[ -d /run/live/rootfs/filesystem.squashfs ]]; then
    SOURCE_COPIE="/run/live/rootfs/filesystem.squashfs"
elif [[ -d /lib/live/mount/rootfs ]]; then
    SOURCE_COPIE="$(find /lib/live/mount/rootfs -maxdepth 1 -type d | tail -1)"
else
    SOURCE_COPIE="/"
fi

rsync -aHAXx --info=progress2 \
    --exclude=/dev/\* --exclude=/proc/\* --exclude=/sys/\* --exclude=/tmp/\* \
    --exclude=/run/\* --exclude=/mnt/\* --exclude=/media/\* \
    --exclude=/var/lib/agentos/\* --exclude=/var/lib/models/\* \
    --exclude=/lost+found \
    "$SOURCE_COPIE/" "$CIBLE_MNT/"

# --- configuration du système installé ------------------------------------

info "configuration"
echo "$NOM_MACHINE" > "$CIBLE_MNT/etc/hostname"
sed -i "s/agent-os/$NOM_MACHINE/g" "$CIBLE_MNT/etc/hosts"

uuid() { blkid -s UUID -o value "$1"; }
cat > "$CIBLE_MNT/etc/fstab" <<EOF
# Système de fichiers d'agent-os.
#
# Les partitions sont désignées par UUID : un nom comme /dev/sda change
# selon l'ordre de détection des disques, et une machine qui démarre sur la
# mauvaise partition ne démarre pas du tout.

UUID=$(uuid "$P_RACINE")   /                  ext4  defaults,noatime            0 1
UUID=$(uuid "$P_EFI")   /boot/efi          vfat  umask=0077                  0 2
UUID=$(uuid "$P_SWAP")   none               swap  sw                          0 0
EOF

if (( CHIFFRER )); then
    cat >> "$CIBLE_MNT/etc/fstab" <<EOF
/dev/mapper/agentos-memoire  /var/lib/agentos  ext4  defaults,noatime  0 2
EOF
    cat > "$CIBLE_MNT/etc/crypttab" <<EOF
agentos-memoire  UUID=$(uuid "$P_MEMOIRE")  none  luks,discard
EOF
else
    cat >> "$CIBLE_MNT/etc/fstab" <<EOF
UUID=$(uuid "$P_MEMOIRE")   /var/lib/agentos   ext4  defaults,noatime            0 2
EOF
fi

cat >> "$CIBLE_MNT/etc/fstab" <<EOF
UUID=$(uuid "$P_MODELES")   /var/lib/models    ext4  defaults,noatime            0 2
EOF

# --- amorçage -------------------------------------------------------------

info "installation de GRUB"
for point in dev dev/pts proc sys; do
    mount --bind "/$point" "$CIBLE_MNT/$point"
done
mount --bind /sys/firmware/efi/efivars "$CIBLE_MNT/sys/firmware/efi/efivars" 2>/dev/null || true

chroot "$CIBLE_MNT" /bin/bash -eu <<EOF
export DEBIAN_FRONTEND=noninteractive

# La session live est propre à l'image : elle n'a rien à faire sur le
# système installé.
systemctl disable getty@tty1 2>/dev/null || true
rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
userdel -r live 2>/dev/null || true
# Le privilège sans mot de passe n'avait de sens que sur l'image.
rm -f /etc/sudoers.d/live

# L'identifiant de machine et les clés SSH sont régénérés : partagés entre
# installations, ils permettraient de se faire passer pour une autre machine.
rm -f /etc/machine-id && systemd-machine-id-setup
ssh-keygen -A

# Jeton de la console, propre à cette machine. L'image n'en transporte
# aucun : un secret identique sur toutes les installations issues de la
# même ISO ne protégerait rien.
jeton="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
sed -i "s|^AGENTOS_API_TOKEN=.*|AGENTOS_API_TOKEN=$jeton|" /etc/agentos/secrets.env

# Le runtime doit démarrer au boot ; en live il était désactivé.
systemctl enable agentos.service agentos-backup.timer systemd-networkd \
    systemd-resolved nftables ssh 2>/dev/null || true
systemctl enable agentos-model.service 2>/dev/null || true

sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=3/' /etc/default/grub 2>/dev/null || true
# resume= pointe le swap : sans lui l'hibernation écrit l'image mais ne sait
# pas où la relire au démarrage suivant.
cat >> /etc/default/grub <<'EOL'
GRUB_CMDLINE_LINUX_DEFAULT="quiet"
GRUB_DISABLE_OS_PROBER=true
EOL
sed -i "s|^GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX=\"resume=UUID=$(blkid -s UUID -o value $P_SWAP)\"|" /etc/default/grub

if [ -d /sys/firmware/efi ]; then
    grub-install --target=x86_64-efi --efi-directory=/boot/efi \
        --bootloader-id=agent-os --recheck
    # Copie de secours à l'emplacement générique : certains micrologiciels
    # ignorent les entrées NVRAM et ne cherchent que ce chemin.
    mkdir -p /boot/efi/EFI/BOOT
    cp /boot/efi/EFI/agent-os/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI
else
    grub-install --target=i386-pc --recheck $CIBLE
fi
update-grub
update-initramfs -u -k all
EOF

# --- comptes --------------------------------------------------------------

echo
info "compte d'administration"
read -rp "Nom d'utilisateur : " admin
while [[ -z "$admin" || ! "$admin" =~ ^[a-z_][a-z0-9_-]*$ ]]; do
    read -rp "Nom invalide. Nom d'utilisateur : " admin
done
chroot "$CIBLE_MNT" useradd -m -s /bin/bash -G sudo,agentos "$admin"
until chroot "$CIBLE_MNT" passwd "$admin"; do
    avert "recommencer"
done

echo
echo "Clé SSH publique de la machine d'administration (vide pour ignorer)."
echo "Sans clé, l'accès distant sera impossible : ce système refuse"
echo "l'authentification par mot de passe."
read -rp "> " cle
if [[ -n "$cle" ]]; then
    chroot "$CIBLE_MNT" install -d -m 0700 -o "$admin" -g "$admin" "/home/$admin/.ssh"
    echo "$cle" > "$CIBLE_MNT/home/$admin/.ssh/authorized_keys"
    chroot "$CIBLE_MNT" chown "$admin:$admin" "/home/$admin/.ssh/authorized_keys"
    chroot "$CIBLE_MNT" chmod 600 "/home/$admin/.ssh/authorized_keys"
    succes "clé installée"
else
    avert "aucune clé : l'accès se fera uniquement en local, au clavier"
fi

# --- fin ------------------------------------------------------------------

jeton="$(grep -oP '(?<=^AGENTOS_API_TOKEN=).*' "$CIBLE_MNT/etc/agentos/secrets.env" || echo '(voir le fichier)')"

info "démontage"
sync
for point in sys/firmware/efi/efivars sys proc dev/pts dev; do
    umount -lf "$CIBLE_MNT/$point" 2>/dev/null || true
done
umount -R "$CIBLE_MNT" 2>/dev/null || true
(( CHIFFRER )) && cryptsetup close agentos-memoire 2>/dev/null || true

succes "agent-os installé sur $CIBLE"
cat <<EOF

  Retirer le support d'installation, puis redémarrer.

  Au premier démarrage :

    1. Renseigner les secrets
         sudo nano /etc/agentos/secrets.env
       — ANTHROPIC_API_KEY pour l'API Claude
       — AGENTOS_REMOTE_URL et AGENTOS_SUPABASE_KEY pour la mémoire distante
       — AGENTOS_REMOTE_KEY, phrase chiffrant la mémoire avant tout envoi

    2. Appliquer le schéma distant, une seule fois pour toute la flotte
         psql "\$DSN" -f /usr/share/doc/agent-os/db/supabase/001_schema.sql
         psql "\$DSN" -f /usr/share/doc/agent-os/db/supabase/002_rls.sql

    3. Vérifier puis redémarrer le service
         sudo agentosctl doctor
         sudo systemctl restart agentos
         agentosctl status

    4. Console locale, depuis la machine ou par tunnel SSH
         ssh -L 8787:127.0.0.1:8787 $admin@$NOM_MACHINE
         http://127.0.0.1:8787
       Jeton : $jeton

  Pour un modèle local, déposer un .gguf dans /var/lib/models puis
    sudo systemctl start agentos-model

EOF
