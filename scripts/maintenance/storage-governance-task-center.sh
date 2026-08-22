#!/usr/bin/env bash
set -Eeuo pipefail

# Task Center 以 yosef 用户运行；实际治理脚本由 root 持有，并通过精确 sudoers 规则调用。
exec sudo -n /usr/local/sbin/markethub-storage-governance "$@"
