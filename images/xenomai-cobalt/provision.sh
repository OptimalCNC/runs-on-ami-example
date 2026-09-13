#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
recipe=/opt/ami-example-recipe
[[ "$EUID" -eq 0 && -f "$recipe/recipe.json" && -f "$recipe/source-inventory.json" ]]
mkdir -p /var/lib/ami-example /opt/ami-example-inputs
python3 "$recipe/images/common/inventory.py" --expect "$recipe/source-inventory.json" > /var/lib/ami-example/parent-inventory.json
if [[ "${STOCK_ONLY:-false}" == true ]]; then
  systemctl is-active ssh
  curl --fail --head --proto '=https' --tlsv1.2 https://snapshot.ubuntu.com/
  exit 0
fi
# Stop timers before touching apt; never upgrade the whole parent distribution.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer || true
systemctl stop apt-daily.service apt-daily-upgrade.service
systemctl mask apt-daily.service apt-daily-upgrade.service unattended-upgrades.service
python3 "$recipe/images/common/install-packages.py" "$recipe/images/xenomai-cobalt/inputs.lock.json" /opt/ami-example-inputs
# Cobalt grants non-root kernel access to this group through the boot command line.
python3 - "$recipe/images/xenomai-cobalt/inputs.lock.json" <<'PY'
import grp
import json
import subprocess
import sys

gid = json.load(open(sys.argv[1]))["xenomai"]["allowed_group_gid"]
try:
    group = grp.getgrnam("xenomai")
except KeyError:
    subprocess.run(["groupadd", "--gid", str(gid), "xenomai"], check=True)
else:
    if group.gr_gid != gid:
        raise ValueError("existing xenomai group has a different GID")
subprocess.run(["usermod", "--append", "--groups", "xenomai", "runner"], check=True)
PY
mkdir -p /etc/security/limits.d /etc/systemd/system.conf.d
cat > /etc/security/limits.d/99-xenomai.conf <<'EOF'
@xenomai - memlock unlimited
@xenomai - rtprio 99
EOF
# RunsOn starts its runner from a system service, which need not open a PAM session.
# These defaults take effect on the new AMI's first boot and cover that ancestry.
cat > /etc/systemd/system.conf.d/99-xenomai.conf <<'EOF'
[Manager]
DefaultLimitMEMLOCK=infinity
DefaultLimitRTPRIO=99
EOF
install -m 0755 "$recipe/images/common/runner-image-env" /usr/local/bin/runner-image-env
install -m 0755 "$recipe/images/xenomai-cobalt/smoke.sh" /usr/local/bin/ami-example-smoke
install -m 0755 "$recipe/images/common/guest-report.py" /usr/local/bin/ami-example-guest-report
bash "$recipe/images/xenomai-cobalt/build-kernel.sh"
# Transfer immutable inputs back over Packer's existing SSM/SSH channel.
tar -C /opt/ami-example-inputs -cf /tmp/ami-example-inputs.tar .
chmod 0644 /tmp/ami-example-inputs.tar
