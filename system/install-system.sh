#!/usr/bin/env bash
# Installe agent-os sur un système Debian existant.
#
# Utilisé de deux façons : par le constructeur d'ISO, dans le chroot de
# l'image, et directement sur une machine Debian déjà en service. Le script
# est idempotent — le relancer après une mise à jour du dépôt remplace le
# runtime sans toucher à la mémoire ni à la configuration.

set -euo pipefail

RACINE="${RACINE:-/}"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UTILISATEUR="agentos"
DEST_PY="${RACINE}usr/lib/python3/dist-packages"

info()   { printf '\033[36m::\033[0m %s\n' "$*"; }
succes() { printf '\033[32mok\033[0m %s\n' "$*"; }
erreur() { printf '\033[31méchec\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || erreur "ce script doit être lancé en root"
[[ -d "$SOURCE/runtime/agentos" ]] || erreur "runtime introuvable dans $SOURCE"

# --- compte de service ----------------------------------------------------
# Un compte système sans interpréteur : il ne sert qu'à faire tourner le
# service, jamais à ouvrir une session.
if ! chroot "$RACINE" id "$UTILISATEUR" >/dev/null 2>&1; then
    info "création du compte $UTILISATEUR"
    chroot "$RACINE" useradd --system --create-home \
        --home-dir /var/lib/agentos \
        --shell /usr/sbin/nologin \
        --comment "agent-os runtime" "$UTILISATEUR"
fi

# --- runtime --------------------------------------------------------------
info "installation du runtime Python"
install -d -m 0755 "$DEST_PY"
rm -rf "${DEST_PY:?}/agentos"
cp -r "$SOURCE/runtime/agentos" "$DEST_PY/agentos"
# Les caches de bytecode d'une autre version de Python provoqueraient des
# erreurs d'import silencieuses au premier démarrage.
find "$DEST_PY/agentos" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod -R a+rX "$DEST_PY/agentos"

info "installation des exécutables"
install -D -m 0755 "$SOURCE/system/bin/agentos-backup" "${RACINE}usr/local/bin/agentos-backup"
cat > "${RACINE}usr/local/bin/agentosctl" <<'EOF'
#!/usr/bin/python3
import sys
from agentos.cli import main
sys.exit(main())
EOF
chmod 0755 "${RACINE}usr/local/bin/agentosctl"

# --- configuration --------------------------------------------------------
install -d -m 0750 "${RACINE}etc/agentos"
chroot "$RACINE" chown root:"$UTILISATEUR" /etc/agentos

if [[ ! -f "${RACINE}etc/agentos/config.toml" ]]; then
    info "configuration par défaut"
    install -m 0640 "$SOURCE/system/config.toml.exemple" "${RACINE}etc/agentos/config.toml"
    chroot "$RACINE" chown root:"$UTILISATEUR" /etc/agentos/config.toml
fi

if [[ ! -f "${RACINE}etc/agentos/secrets.env" ]]; then
    # Le jeton de la console est tiré au hasard à l'installation : une valeur
    # par défaut partagée par toutes les images serait pire que pas de jeton
    # du tout, puisqu'elle donnerait l'illusion d'une protection.
    jeton="$(chroot "$RACINE" python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    cat > "${RACINE}etc/agentos/secrets.env" <<EOF
# Secrets d'agent-os. Fichier 0640, root:agentos — ne jamais le committer.
#
# Clé de l'API Claude. Sans elle, seul le modèle local est utilisé.
ANTHROPIC_API_KEY=

# Jeton de la console locale, tiré au hasard à l'installation.
AGENTOS_API_TOKEN=$jeton

# Mémoire distante Supabase. La clé doit être « service_role », jamais
# « anon » : RLS refuse tout à cette dernière, par construction.
AGENTOS_REMOTE_URL=
AGENTOS_SUPABASE_KEY=

# Phrase secrète chiffrant la mémoire avant tout envoi. Sans elle et avec
# remote.encrypt = true, la synchro refuse de démarrer plutôt que de partir
# en clair.
AGENTOS_REMOTE_KEY=
EOF
    chmod 0640 "${RACINE}etc/agentos/secrets.env"
    chroot "$RACINE" chown root:"$UTILISATEUR" /etc/agentos/secrets.env
    succes "jeton de console généré (voir /etc/agentos/secrets.env)"
fi

# --- intégration système --------------------------------------------------
info "unités systemd et réglages noyau"
install -D -m 0644 "$SOURCE/system/systemd/agentos.service"        "${RACINE}etc/systemd/system/agentos.service"
install -D -m 0644 "$SOURCE/system/systemd/agentos-model.service"  "${RACINE}etc/systemd/system/agentos-model.service"
install -D -m 0644 "$SOURCE/system/systemd/agentos-backup.service" "${RACINE}etc/systemd/system/agentos-backup.service"
install -D -m 0644 "$SOURCE/system/systemd/agentos-backup.timer"   "${RACINE}etc/systemd/system/agentos-backup.timer"
install -D -m 0644 "$SOURCE/system/tmpfiles.d/agentos.conf"        "${RACINE}usr/lib/tmpfiles.d/agentos.conf"
install -D -m 0644 "$SOURCE/system/sysctl.d/99-agentos.conf"       "${RACINE}etc/sysctl.d/99-agentos.conf"
install -D -m 0644 "$SOURCE/system/nftables/agentos.nft"           "${RACINE}etc/nftables.conf"

install -d -m 0755 "${RACINE}usr/share/doc/agent-os"
cp "$SOURCE/README.md" "${RACINE}usr/share/doc/agent-os/" 2>/dev/null || true
cp -r "$SOURCE/docs" "${RACINE}usr/share/doc/agent-os/" 2>/dev/null || true
cp -r "$SOURCE/db"   "${RACINE}usr/share/doc/agent-os/" 2>/dev/null || true

# --- répertoires d'état ---------------------------------------------------
for repertoire in var/lib/agentos var/lib/agentos/workspace var/lib/agentos/sauvegardes \
                  var/lib/models var/log/agentos; do
    install -d -m 0750 "${RACINE}${repertoire}"
    chroot "$RACINE" chown "$UTILISATEUR:$UTILISATEUR" "/$repertoire"
done
chmod 0755 "${RACINE}var/lib/models"

# --- activation -----------------------------------------------------------
# Dans un chroot de construction d'image, systemd ne tourne pas : on se
# contente de poser les liens que `systemctl enable` aurait créés.
if [[ "$RACINE" == "/" ]] && chroot "$RACINE" systemctl is-system-running >/dev/null 2>&1; then
    info "activation des services"
    systemctl daemon-reload
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/agentos.conf
    sysctl --quiet --load /etc/sysctl.d/99-agentos.conf || true
    systemctl enable --now agentos.service agentos-backup.timer
    systemctl enable agentos-model.service || true
else
    info "activation différée (pas de systemd actif : image en construction)"
    chroot "$RACINE" systemctl enable agentos.service agentos-backup.timer \
        agentos-model.service 2>/dev/null || {
        install -d "${RACINE}etc/systemd/system/multi-user.target.wants"
        ln -sf /etc/systemd/system/agentos.service \
            "${RACINE}etc/systemd/system/multi-user.target.wants/agentos.service"
        install -d "${RACINE}etc/systemd/system/timers.target.wants"
        ln -sf /etc/systemd/system/agentos-backup.timer \
            "${RACINE}etc/systemd/system/timers.target.wants/agentos-backup.timer"
    }
fi

succes "agent-os installé"
cat <<'EOF'

Étapes suivantes :
  1. Renseigner /etc/agentos/secrets.env (clé API, Supabase, phrase de chiffrement)
  2. systemctl restart agentos
  3. agentosctl doctor       — vérifier que tout est en place
  4. agentosctl status       — état du runtime
  5. Console : http://127.0.0.1:8787 (jeton dans secrets.env)

Pour un modèle local, déposer un fichier .gguf dans /var/lib/models puis :
  systemctl start agentos-model
EOF
