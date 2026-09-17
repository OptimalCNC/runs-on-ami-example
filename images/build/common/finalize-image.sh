#!/bin/bash
set -euo pipefail
# Finalize the Ubuntu 24.04 image after installing its runner and Cobalt payload.
recipe=/opt/ami-example-recipe
[[ "$EUID" -eq 0 && -f "$recipe/images/build/xenomai-cobalt/inputs.lock.json" ]]
python3 - "$recipe/images/build/xenomai-cobalt/inputs.lock.json" <<'PY'
import json
import sys
from pathlib import Path
registered = any((base / name).exists()
                 for base in (Path('/home/runner'), Path('/root'), Path('/opt/actions-runner'))
                 for name in ('.runner', '.credentials', '.credentials_rsaparams'))
workspaces = any(p.exists() and any(p.iterdir())
                 for p in (Path('/home/runner/_work'), Path('/opt/actions-runner/_work')))
secure_boot = any(p.read_bytes()[4:5] == b'\x01' for p in Path('/sys/firmware/efi/efivars').glob('SecureBoot-*'))
if registered or workspaces or secure_boot:
    raise SystemExit('refusing to snapshot registered runner, workspace, or Secure Boot image')
version = json.loads(Path(sys.argv[1]).read_text())['runner']['bootstrap']['version']
if not Path(f'/usr/local/bin/runs-on-bootstrap-v{version}').is_file():
    raise SystemExit('RunsOn bootstrap is missing')
PY
apt-get clean
rm -rf /var/lib/apt/lists/* /opt/ami-example-recipe
umount /mnt/ami-example-build
rmdir /mnt/ami-example-build
rm -rf /home/runner/_diag
for user_home in /root /home/ubuntu /home/runner; do
  rm -rf "$user_home/.aws" "$user_home/.docker" "$user_home/.cache" "$user_home/.git-credentials"
  rm -rf "$user_home/.config/gh" "$user_home/.config/git/credentials"
  rm -f "$user_home/.bash_history" "$user_home/.zsh_history"
  if [[ -d "$user_home/.ssh" ]]; then
    find "$user_home/.ssh" -maxdepth 1 -type f -delete
  fi
done
rm -f /etc/ssh/ssh_host_*
rm -rf /var/lib/amazon/ssm/* /var/log/amazon/ssm/*
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
df --block-size=1 --output=source,fstype,size,used,avail /
