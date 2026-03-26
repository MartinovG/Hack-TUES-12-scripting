#!/bin/bash

apt update
apt install -y openssh-server

# Create safe user
useradd -m student
echo "student:password" | chpasswd

# Disable root login
sed -i 's/PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config

systemctl restart ssh