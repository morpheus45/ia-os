#!/bin/sh
# Réseau : DHCP sur toute interface câblée, résolution par systemd-resolved.
#
# Le nom d'interface n'est pas connu à l'avance et change d'une machine à
# l'autre ; la correspondance par motif évite d'avoir à le deviner.
set -eu

mkdir -p /etc/systemd/network
cat > /etc/systemd/network/20-cable.network <<'EOL'
[Match]
Name=en* eth*

[Network]
DHCP=yes
IPv6AcceptRA=yes

[DHCPv4]
UseDomains=yes
# Sur une machine qui redémarre rarement, un bail court évite de garder une
# adresse que le routeur a réattribuée entre-temps.
RouteMetric=100
EOL

systemctl enable systemd-networkd systemd-resolved

# SSH par clé seulement : un mot de passe sur une machine qui tourne seule
# finit toujours par être faible ou réutilisé.
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-agentos.conf <<'EOL'
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
X11Forwarding no
AllowTcpForwarding yes
MaxAuthTries 3
EOL
