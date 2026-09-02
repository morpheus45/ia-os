#!/bin/sh
# Réduction de la surface d'attaque.
set -eu

# Les services qui n'ont rien à faire sur cette machine. Les désactiver
# plutôt que les désinstaller : certains sont des dépendances.
for service in avahi-daemon cups bluetooth ModemManager; do
    systemctl disable "$service" 2>/dev/null || true
    systemctl mask "$service" 2>/dev/null || true
done

systemctl enable nftables 2>/dev/null || true

# Les modules de systèmes de fichiers et de protocoles exotiques sont une
# source récurrente de failles, pour un usage nul ici.
cat > /etc/modprobe.d/agentos-blacklist.conf <<'EOL'
install cramfs /bin/true
install freevxfs /bin/true
install jffs2 /bin/true
install hfs /bin/true
install hfsplus /bin/true
install udf /bin/true
install dccp /bin/true
install sctp /bin/true
install rds /bin/true
install tipc /bin/true
install firewire-core /bin/true
EOL

# Les journaux persistent d'un démarrage à l'autre — sans quoi il devient
# impossible de comprendre pourquoi la machine a redémarré la nuit dernière.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/agentos.conf <<'EOL'
[Journal]
Storage=persistent
SystemMaxUse=500M
SystemMaxFileSize=50M
MaxRetentionSec=1month
EOL

# Limites : un processus emballé ne doit pas pouvoir épuiser la table des
# processus et empêcher l'administrateur de se connecter pour le tuer.
cat > /etc/security/limits.d/agentos.conf <<'EOL'
*  soft  nofile  8192
*  hard  nofile  65536
*  soft  nproc   4096
*  hard  nproc   8192
EOL
