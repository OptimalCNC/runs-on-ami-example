#!/bin/bash
set -euo pipefail
# Direct Packer launch supplies no RunsOn registration user-data.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer
systemctl stop apt-daily.service apt-daily-upgrade.service
if command -v snap >/dev/null; then
  snap refresh --hold=forever
fi
systemctl start ssh
if systemctl list-unit-files amazon-ssm-agent.service --no-legend | grep -q amazon-ssm-agent; then
  systemctl start amazon-ssm-agent
else
  systemctl start snap.amazon-ssm-agent.amazon-ssm-agent.service
fi
