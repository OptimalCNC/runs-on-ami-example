#!/bin/bash
set -euo pipefail
# This cleanup contract is qualified only against the locked Ubuntu 24 RunsOn parent.
recipe=/opt/ami-example-recipe
[[ "$EUID" -eq 0 && -f "$recipe/recipe.json" && -f /etc/ami-example.json ]]
python3 "$recipe/images/common/inventory.py" > /tmp/ami-example-final-inventory.json
python3 - <<'PY'
import json
from pathlib import Path
i = json.loads(Path('/tmp/ami-example-final-inventory.json').read_text())
if i['registered'] or i['workspaces'] or i['secure_boot']:
    raise SystemExit('refusing to snapshot registered runner, workspace, or Secure Boot image')
if not i['bootstrap_files']:
    raise SystemExit('RunsOn bootstrap is missing')
PY
rm -rf /usr/src/ami-example /opt/ami-example-inputs /opt/ami-example-recipe
rm -rf /home/runner/_diag
rm -f /tmp/ami-example-inputs.tar /tmp/ami-example-final-inventory.json
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
