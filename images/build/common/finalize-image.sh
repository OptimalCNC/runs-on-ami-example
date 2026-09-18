#!/bin/bash
set -euo pipefail
# Finalize the Ubuntu 24.04 image after installing its runner and Cobalt payload.
apt-get clean
rm -rf /var/lib/apt/lists/* /opt/ami-example-recipe
umount /mnt/ami-example-build
rmdir /mnt/ami-example-build
rm -rf /root/.ssh /home/ubuntu/.ssh /home/runner/.ssh
rm -f /etc/ssh/ssh_host_*
# cloud-init must regenerate instance state, host keys and machine-id at the next boot.
cloud-init clean --logs --machine-id --seed --configs network
rm -f /var/lib/dbus/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
rm -f /var/lib/systemd/random-seed
rm -rf /var/log/journal/*
# O_CREAT can be denied for service-owned logs by fs.protected_regular.
find /var/log -type f -exec truncate --no-create -s 0 {} +
find /tmp /var/tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} +
sync
fstrim /
