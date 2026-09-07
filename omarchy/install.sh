#!/usr/bin/env bash
# Rend une machine Omarchy autonome : installe agent-os et l'intègre au bureau.
#
# Une seule machine. Le service démarre au boot, avant et indépendamment de
# toute session graphique — c'est ce que veut dire « autonome ». Le bureau
# n'est pas remplacé : il gagne une barre, trois raccourcis et des
# notifications.
#
#   ./install.sh
#
# Ce qui est posé sur le système (avec sudo) : le runtime, les unités
# systemd, la configuration. Ce qui est posé chez toi : les exécutables de
# bureau, le module waybar, les raccourcis Hyprland.

set -euo pipefail

ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RACINE_PROJET="$(cd "$ICI/.." && pwd)"
BIN="$HOME/.local/bin"
CONFIG="$HOME/.config/agentos"
MARQUEUR="/* agent-os */"

info()   { printf '\033[36m::\033[0m %s\n' "$*"; }
succes() { printf '\033[32mok\033[0m %s\n' "$*"; }
avert()  { printf '\033[33m! \033[0m %s\n' "$*"; }
erreur() { printf '\033[31méchec\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || erreur "à lancer en utilisateur ordinaire — le script appelle sudo lui-même"
command -v sudo >/dev/null || erreur "sudo est requis"
command -v python3 >/dev/null || erreur "python3 est requis"

command -v hyprctl >/dev/null 2>&1 || avert \
    "hyprctl introuvable : ce poste ne semble pas être sous Hyprland.
    Le service s'installera, l'intégration au bureau non."

# --- 1. le système --------------------------------------------------------

info "installation du runtime et des services"
sudo bash "$RACINE_PROJET/system/install-system.sh"

# --- 2. cohabitation avec le bureau ---------------------------------------

info "bornes mémoire adaptées à un poste de travail"
for paire in "agentos:bureau-agentos.conf" "agentos-model:bureau-modele.conf"; do
    service="${paire%%:*}"; fichier="${paire##*:}"
    sudo install -D -m 0644 "$ICI/systemd/$fichier" \
        "/etc/systemd/system/$service.service.d/bureau.conf"
done
sudo install -m 0644 "$ICI/systemd/modele-bureau.env" /etc/agentos/model.env
sudo systemctl daemon-reload
succes "runtime plafonné à 2 Gio, modèle local à 7 Gio"

# --- 3. secrets -----------------------------------------------------------

if ! sudo grep -q '^ANTHROPIC_API_KEY=.\+' /etc/agentos/secrets.env 2>/dev/null; then
    echo
    echo "  Clé de l'API Claude — laisse vide pour n'utiliser qu'un modèle local."
    echo "  Elle sera écrite dans /etc/agentos/secrets.env (0640, root:agentos)."
    read -rsp "  Clé : " cle; echo
    if [[ -n "$cle" ]]; then
        sudo sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$cle|" /etc/agentos/secrets.env
        succes "clé enregistrée"
    else
        avert "sans clé, l'agent ne fonctionnera qu'avec un modèle local
    (déposer un .gguf dans /var/lib/models)"
    fi
fi

# Appartenir au groupe agentos permet de lire la configuration et les
# secrets sans sudo. Le changement ne prend effet qu'à la prochaine
# ouverture de session — d'où la lecture par sudo un peu plus bas.
if ! id -nG "$USER" | grep -qw agentos; then
    sudo usermod -aG agentos "$USER"
    avert "ajouté au groupe agentos — effectif à ta prochaine connexion"
fi

# --- 4. démarrage ---------------------------------------------------------

info "démarrage du service"
sudo systemctl enable --now agentos.service
sleep 2
sudo systemctl is-active --quiet agentos.service \
    && succes "agent-os tourne, et repartira à chaque démarrage" \
    || avert "le service n'est pas actif — journalctl -u agentos -n 40"

# --- 5. intégration au bureau ---------------------------------------------

info "outils de bureau dans $BIN"
install -d "$BIN"
install -m 0755 "$ICI/bin/agentos-desktop"        "$BIN/agentos-desktop"
install -m 0755 "$ICI/bin/agentos-waybar-install" "$BIN/agentos-waybar-install"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) avert "$BIN n'est pas dans le PATH — ajouter à ~/.bashrc :
    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

info "configuration du client"
install -d -m 0700 "$CONFIG"
jeton="$(sudo sed -n 's/^AGENTOS_API_TOKEN=//p' /etc/agentos/secrets.env | head -1)"
port="$(sudo sed -n 's/^port *= *//p' /etc/agentos/config.toml | head -1 | tr -d ' ')"
umask 077
cat > "$CONFIG/remote.conf" <<EOF
# Liaison vers le service agent-os de cette machine.
#
# AGENTOS_SSH est vide : le service est local, il n'y a pas de tunnel.
# Le renseigner ferait pointer les outils de bureau vers une machine
# distante, à condition qu'un tunnel ramène son port ici.

AGENTOS_SSH=
AGENTOS_PORT=${port:-8787}
AGENTOS_TOKEN=$jeton
AGENTOS_NOM=agent-os

# Au-delà de cette durée, la fin d'un travail donne lieu à une
# notification. En dessous, l'entretien périodique noierait l'utile.
AGENTOS_SEUIL_LONG_S=60
EOF
umask 022
succes "$CONFIG/remote.conf (0600)"

# --- 6. barre -------------------------------------------------------------

info "module waybar"
if "$BIN/agentos-waybar-install"; then
    style="$HOME/.config/waybar/style.css"
    if [[ -f "$style" ]] && ! grep -qF "$MARQUEUR" "$style"; then
        printf '\n%s\n' "$MARQUEUR" >> "$style"
        cat "$ICI/waybar/style.css" >> "$style"
        succes "styles ajoutés"
    fi
    pkill -SIGUSR2 waybar 2>/dev/null && info "waybar rechargée" || true
else
    avert "module non installé — l'ajouter à la main depuis $ICI/waybar/agentos.jsonc"
fi

# --- 7. raccourcis --------------------------------------------------------

info "raccourcis Hyprland"
install -d "$HOME/.config/hypr"
install -m 0644 "$ICI/hypr/agentos.conf" "$HOME/.config/hypr/agentos.conf"

bindings="$HOME/.config/hypr/bindings.conf"
touch "$bindings"
if grep -qF "agentos.conf" "$bindings"; then
    info "déjà sourcé depuis bindings.conf"
else
    # En fin de fichier : les raccourcis d'agent-os passent après ceux
    # d'Omarchy et ne peuvent donc pas en masquer un silencieusement.
    printf '\n# agent-os\nsource = ~/.config/hypr/agentos.conf\n' >> "$bindings"
    succes "sourcé depuis $bindings"
fi
hyprctl reload >/dev/null 2>&1 && info "Hyprland rechargé" || true

# --- 8. vérification ------------------------------------------------------

echo
info "vérification"
"$BIN/agentos-desktop" check || avert "le service met parfois quelques secondes à répondre"

cat <<'EOF'

  La machine est autonome : le service démarre au boot, travaille seul,
  et se souvient d'un redémarrage à l'autre.

  Raccourcis :

    SUPER+ALT+A    confier une tâche
    SUPER+ALT+M    menu des actions
    SUPER+ALT+C    console web

  Barre : clic gauche = menu, clic droit = tâche, clic milieu = console.

  En ligne de commande :

    agentosctl status              état complet
    agentosctl ask "…"             confier une tâche et attendre
    agentos-desktop ask "…"        confier une tâche en arrière-plan
    agentosctl doctor              diagnostic

  Modèle local hors-ligne — déposer un GGUF de 7 milliards de paramètres
  quantifié en 4 bits (environ 4,5 Go), puis :

    sudo -u agentos curl -L -o /var/lib/models/modele.gguf "<url>"
    sudo systemctl start agentos-model

EOF
