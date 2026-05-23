#!/usr/bin/env bash
# bootstrap-server.sh
# Run ONCE on a fresh Ubuntu VPS as root.  After this, Ansible takes over.
#
# Usage (from your laptop):
#     scp bootstrap-server.sh root@<VPS_IP>:/root/
#     ssh root@<VPS_IP> 'bash /root/bootstrap-server.sh'
#
# What this does:
#   1. Creates a `deploy` user with passwordless sudo
#   2. Copies your authorized_keys to that user
#   3. Installs Python 3 (Ansible's only requirement on the target)
#   4. Hardens sshd: no root login, no password auth
#   5. Installs UFW with sane defaults (ssh + http + https)
#
# After this script, your laptop reaches the box as `deploy@<IP>` only.
# The Ansible playbook does everything else.

set -euo pipefail

DEPLOY_USER="deploy"

if [[ $EUID -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

echo "[bootstrap] creating user '$DEPLOY_USER'..."
if ! id -u "$DEPLOY_USER" &>/dev/null; then
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
usermod -aG sudo "$DEPLOY_USER"
echo "$DEPLOY_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$DEPLOY_USER"
chmod 440 "/etc/sudoers.d/90-$DEPLOY_USER"

echo "[bootstrap] copying root's authorized_keys to '$DEPLOY_USER'..."
mkdir -p "/home/$DEPLOY_USER/.ssh"
chmod 700 "/home/$DEPLOY_USER/.ssh"
if [[ -f /root/.ssh/authorized_keys ]]; then
    cp /root/.ssh/authorized_keys "/home/$DEPLOY_USER/.ssh/authorized_keys"
fi
chmod 600 "/home/$DEPLOY_USER/.ssh/authorized_keys"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"

echo "[bootstrap] installing python3 (Ansible target requirement)..."
apt-get update -y >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-apt sudo >/dev/null

echo "[bootstrap] hardening sshd (no root login, no password auth)..."
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
systemctl reload ssh || systemctl reload sshd

echo "[bootstrap] installing + configuring UFW..."
DEBIAN_FRONTEND=noninteractive apt-get install -y ufw >/dev/null
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow http
ufw allow https
ufw --force enable
ufw status verbose

echo
echo "[bootstrap] DONE."
echo "  Verify from your laptop:"
echo "      ssh ${DEPLOY_USER}@<VPS_IP> 'whoami; sudo -n true && echo sudo-ok'"
echo "  Then run the Ansible playbook from your laptop."
