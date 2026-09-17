#!/usr/bin/env bash
# Assistant d'installation d'agent-os.
#
# S'ouvre tout seul au démarrage de l'image. Il ne fait rien que
# « agentos-installer » ne sache faire : il pose les questions à la place de
# l'utilisateur et lui évite d'avoir à connaître les noms des disques sous
# Linux. Toute la partie dangereuse — partitionnement, formatage, amorceur —
# reste dans l'installateur, seul endroit où elle est écrite.
#
# Pas de « set -e » : un assistant qui disparaît au premier grep infructueux
# laisse l'utilisateur devant un écran noir sans rien lui dire.
set -uo pipefail

C_TITRE=$'\033[1;36m'; C_OK=$'\033[32m'; C_ALERTE=$'\033[33m'
C_DANGER=$'\033[31m'; C_ATONE=$'\033[2m'; C_FIN=$'\033[0m'

titre() {
    printf '\n%s  %s%s\n' "$C_TITRE" "$1" "$C_FIN"
    printf '%s  %s%s\n\n' "$C_ATONE" "${1//?/-}" "$C_FIN"
}
info()   { printf '  %s\n' "$*"; }
ok()     { printf '  %s%s%s\n' "$C_OK" "$*" "$C_FIN"; }
alerte() { printf '  %s%s%s\n' "$C_ALERTE" "$*" "$C_FIN"; }
danger() { printf '  %s%s%s\n' "$C_DANGER" "$*" "$C_FIN"; }
atone()  { printf '  %s%s%s\n' "$C_ATONE" "$*" "$C_FIN"; }

pause() { printf '\n'; read -rsp "  Entrée pour continuer... " -n1 _ 2>/dev/null; printf '\n'; }

# Sur un clavier français, les chiffres de la rangée du haut demandent la
# touche majuscule. Accepter aussi ce que donne la même touche sans elle
# évite de renvoyer « choix invalide » à quelqu'un qui a visé juste.
normaliser_chiffre() {
    case "$1" in
        '&') echo 1 ;; 'é') echo 2 ;; '"') echo 3 ;; "'") echo 4 ;;
        '(') echo 5 ;; '-') echo 6 ;; 'è') echo 7 ;; '_') echo 8 ;;
        'ç') echo 9 ;; 'à') echo 0 ;;
        *)   echo "$1" ;;
    esac
}

oui() {
    local reponse
    read -rp "  $1 [o/N] " reponse
    [[ "${reponse,,}" =~ ^(o|oui|y|yes)$ ]]
}

# --- disques --------------------------------------------------------------

# Le support depuis lequel on a démarré ne doit pas être proposé : l'effacer
# tuerait le système en train de tourner. L'installateur le refuse déjà, mais
# le proposer puis le refuser est une impasse ; mieux vaut ne pas le proposer.
supports_du_systeme() {
    local point source parent type
    for point in / /run/live/medium /lib/live/mount/medium /boot/efi; do
        source="$(findmnt -no SOURCE "$point" 2>/dev/null | head -1)"
        # Écarte au passage overlay, tmpfs et les autres sources qui ne sont
        # pas des périphériques bloc.
        [[ -b "$source" ]] || continue
        parent="$(lsblk -no PKNAME "$source" 2>/dev/null | head -1)"
        if [[ -n "$parent" ]]; then
            echo "/dev/$parent"
        else
            # Une racine peut vivre directement sur un disque entier, sans
            # table de partitions : il n'y a alors pas de parent, et c'est le
            # disque lui-même qu'il faut écarter.
            type="$(lsblk -dno TYPE "$source" 2>/dev/null | head -1)"
            [[ "$type" == disk ]] && echo "$source"
        fi
    done
}

# Le minimum qu'exige l'installateur, en octets. L'annoncer ici évite de
# laisser choisir un disque qui sera refusé trois écrans plus loin.
MINIMUM_OCTETS=$((120 * 1024 * 1024 * 1024))

trop_petit() {
    local octets
    octets="$(lsblk -dbno SIZE "$1" 2>/dev/null | head -1)"
    [[ -n "$octets" ]] || return 1
    (( octets < MINIMUM_OCTETS ))
}

decrire_disque() {
    local disque="$1" contenu=""
    local taille modele transport
    taille="$(lsblk -dno SIZE "$disque" 2>/dev/null | tr -d ' ')"
    modele="$(lsblk -dno MODEL "$disque" 2>/dev/null | sed 's/  */ /g;s/^ *//;s/ *$//')"
    transport="$(lsblk -dno TRAN "$disque" 2>/dev/null | head -1)"

    [[ -n "$modele" ]] || modele="(sans nom)"
    case "$transport" in
        usb) contenu="branché en USB" ;;
        "")  contenu="" ;;
        *)   contenu="$transport" ;;
    esac

    printf '%-12s %8s  %s' "$disque" "$taille" "$modele"
    [[ -n "$contenu" ]] && printf '  %s(%s)%s' "$C_ATONE" "$contenu" "$C_FIN"
    printf '\n'
}

porte_windows() {
    lsblk -no FSTYPE "$1" 2>/dev/null | grep -qi ntfs && return 0
    lsblk -no PARTTYPENAME "$1" 2>/dev/null | grep -qi microsoft && return 0
    return 1
}

# --- choix du disque ------------------------------------------------------

DISQUE_CHOISI=""

choisir_disque() {
    DISQUE_CHOISI=""
    local exclus disponibles=() disque
    exclus="$(supports_du_systeme)"

    while read -r disque; do
        [[ -n "$disque" ]] || continue
        # zram et les disques en mémoire vive portent le type « disk » mais
        # disparaissent à l'extinction : les proposer n'a aucun sens.
        [[ "$disque" == /dev/zram* || "$disque" == /dev/ram* ]] && continue
        grep -qxF "$disque" <<<"$exclus" && continue
        disponibles+=("$disque")
    done < <(lsblk -dpno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}')

    titre "Sur quel disque installer agent-os ?"

    if (( ${#disponibles[@]} == 0 )); then
        danger "Aucun disque disponible."
        echo
        atone "Seul le support de démarrage est visible. Si le disque visé est"
        atone "externe, le brancher puis revenir à ce menu."
        pause
        return 1
    fi

    local i=1
    for disque in "${disponibles[@]}"; do
        printf '  %s%d)%s ' "$C_TITRE" "$i" "$C_FIN"
        decrire_disque "$disque"
        if porte_windows "$disque"; then
            printf '       %sCE DISQUE CONTIENT WINDOWS%s\n' "$C_DANGER" "$C_FIN"
        fi
        if trop_petit "$disque"; then
            printf '       %strop petit — il en faut 120 Gio%s\n' "$C_ATONE" "$C_FIN"
        fi
        i=$((i + 1))
    done

    echo
    atone "Le support de démarrage n'est pas dans la liste : il ne peut pas"
    atone "s'effacer lui-même."
    echo

    local choix
    read -rp "  Numéro du disque (vide pour revenir) : " choix
    [[ -n "$choix" ]] || return 1
    choix="$(normaliser_chiffre "$choix")"

    if ! [[ "$choix" =~ ^[0-9]+$ ]] || (( choix < 1 || choix > ${#disponibles[@]} )); then
        danger "Choix invalide."
        pause
        return 1
    fi

    local retenu="${disponibles[$((choix - 1))]}"
    if trop_petit "$retenu"; then
        danger "$retenu est trop petit : il faut au moins 120 Gio."
        atone "La mémoire de l'agent et les modèles locaux n'y tiendraient pas."
        pause
        return 1
    fi

    DISQUE_CHOISI="$retenu"
    return 0
}

# --- nature du support ----------------------------------------------------

# Un disque débranchable se configure autrement qu'un disque interne. Le
# noyau le dit — sauf dans une machine virtuelle, où l'hyperviseur présente
# tous les disques de la même façon. Là, seul l'utilisateur sait.
OPTION_SUPPORT=""

# L'assistant peut se retrouver devant un installateur plus ancien que lui :
# il se télécharge à l'unité, l'image se grave une fois pour toutes. Lui
# passer une option qu'il ne connaît pas le ferait échouer sur « option
# inconnue » sans que personne comprenne pourquoi.
installateur_gere_support() {
    agentos-installer --aide 2>/dev/null | grep -q -- '--externe'
}

determiner_support() {
    local disque="$1" transport
    transport="$(lsblk -dno TRAN "$disque" 2>/dev/null | head -1)"

    local virtualise=0
    if command -v systemd-detect-virt >/dev/null 2>&1 \
       && systemd-detect-virt --quiet 2>/dev/null; then
        virtualise=1
    fi

    if (( ! virtualise )); then
        if [[ "$transport" == usb ]]; then
            OPTION_SUPPORT="--externe"
            ok "Disque externe reconnu : démarrage adapté, hibernation désactivée."
        else
            OPTION_SUPPORT="--interne"
        fi
        return 0
    fi

    titre "Ce disque est-il un disque externe ?"
    info "L'installation se fait depuis une machine virtuelle. Celle-ci"
    info "présente tous les disques comme des disques internes : impossible"
    info "de deviner comment celui-ci est branché sur ton ordinateur."
    echo
    atone "Un disque externe a besoin d'un délai d'attente au démarrage. Sans"
    atone "lui, le système s'arrêtera au premier démarrage sur une erreur"
    atone "« UUID does not exist » — longtemps après l'installation."
    echo

    if oui "Ce disque est-il branché en USB (disque externe) ?"; then
        OPTION_SUPPORT="--externe"
        ok "Traité comme un disque externe."
    else
        OPTION_SUPPORT="--interne"
        ok "Traité comme un disque interne."
    fi
}

# Renvoie 1 quand il vaut mieux ne rien écrire du tout.
verifier_accord_installateur() {
    installateur_gere_support && return 0

    # L'installateur est ancien : il déduira lui-même, et il déduit bien
    # partout sauf dans une machine virtuelle. Là, et seulement là, le
    # résultat est un système qui ne démarrera pas.
    if [[ "$OPTION_SUPPORT" == "--externe" ]]; then
        titre "Cette image est trop ancienne pour ce disque"
        danger "L'installateur de cette image ne sait pas qu'on peut lui"
        danger "désigner un disque externe."
        echo
        info "Il déduira « interne » — la machine virtuelle ne lui montre rien"
        info "d'autre — et le système installé s'arrêtera au premier démarrage"
        info "sur « ALERT! UUID=... does not exist »."
        echo
        info "Récupérer une image récente, ou installer depuis une clé USB"
        info "démarrée sur l'ordinateur : là, la déduction est juste."
        echo
        oui "Installer quand même, en sachant que ça ne démarrera pas ?" \
            || return 1
    fi

    OPTION_SUPPORT=""
    return 0
}

# --- installation ---------------------------------------------------------

lancer_installation() {
    choisir_disque || return 0
    local disque="$DISQUE_CHOISI"

    if porte_windows "$disque"; then
        titre "Attention"
        danger "$disque contient des partitions Windows."
        echo
        info "Si c'est le disque de ton système, Windows sera détruit."
        info "Une lettre « D: » peut désigner une partition du MÊME disque"
        info "physique que « C: »."
        echo
        oui "Continuer quand même ?" || return 0
    fi

    determiner_support "$disque"
    verifier_accord_installateur || { pause; return 0; }

    titre "Plan d'installation"
    atone "Rien n'est écrit à cette étape."
    echo
    sudo agentos-installer -d "$disque" ${OPTION_SUPPORT:+"$OPTION_SUPPORT"} --simulation
    local code=$?
    if (( code != 0 )); then
        echo
        danger "Le plan n'a pas pu être établi. Rien n'a été écrit."
        pause
        return 0
    fi

    echo
    info "Vérifier avant de continuer :"
    info "  - la taille affichée est bien celle du disque visé"
    info "  - la ligne « Support » correspond à la réalité"
    echo

    if ! oui "Lancer l'installation ? Le disque sera effacé."; then
        info "Annulé. Rien n'a été écrit."
        pause
        return 0
    fi

    titre "Installation"
    sudo agentos-installer -d "$disque" ${OPTION_SUPPORT:+"$OPTION_SUPPORT"}
    code=$?
    echo
    if (( code != 0 )); then
        danger "L'installation a échoué."
        atone "Le détail est au-dessus. Le disque est probablement à moitié"
        atone "partitionné : relancer l'installation le remettra à plat."
        pause
        return 0
    fi

    ok "agent-os est installé sur $disque."
    echo
    info "Retirer le support d'installation, puis redémarrer et ouvrir le"
    info "menu d'amorçage — F12 chez Dell et Lenovo, F9 chez HP, Échap"
    info "ailleurs — pour choisir ce disque."
    echo
    if oui "Éteindre maintenant ?"; then
        sudo systemctl poweroff
    fi
    pause
}

# --- menu -----------------------------------------------------------------

menu() {
    while true; do
        clear 2>/dev/null
        cat <<'ENTETE'

  agent-os — image d'installation

  Ce support ne touche à rien tant qu'on ne le lui demande pas.

ENTETE
        printf '  %s1)%s Installer agent-os sur un disque\n' "$C_TITRE" "$C_FIN"
        printf '  %s2)%s Voir ce que la machine détecte\n' "$C_TITRE" "$C_FIN"
        printf '  %s3)%s Garder la mémoire sur ce support, sans installer\n' "$C_TITRE" "$C_FIN"
        printf '  %s4)%s Ouvrir un terminal\n' "$C_TITRE" "$C_FIN"
        printf '  %s5)%s Éteindre\n' "$C_TITRE" "$C_FIN"
        echo

        local choix
        read -rp "  Ton choix : " choix
        choix="$(normaliser_chiffre "$choix")"

        case "$choix" in
            1) lancer_installation ;;
            2) titre "Matériel détecté"; agentos-materiel; pause ;;
            3) titre "Persistance"
               atone "Crée une partition de mémoire dans l'espace libre du support"
               atone "de démarrage. Rien n'est écrit sur l'ordinateur."
               echo
               sudo agentos-persistance; pause ;;
            4) titre "Terminal"
               info "L'assistant se relance avec : agentos-bienvenue"
               echo
               return 0 ;;
            5) sudo systemctl poweroff ;;
            "") ;;
            *) danger "Choix invalide."; pause ;;
        esac
    done
}

menu
