#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
scripts=(install.sh ct/minecraft.sh install/minecraft-install.sh scripts/minecraft tools/check.sh)
for script in "${scripts[@]}"; do
  bash -n "$script"
done
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -v
bash install.sh --help >/dev/null
bash ct/minecraft.sh --help >/dev/null
python3 scripts/manage.py --help >/dev/null
if [[ ${1:-} == --offline ]]; then
  echo 'Offline checks passed. ShellCheck, shfmt and systemd validation were not run.'
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo 'Usage: tools/check.sh [--offline]' >&2
  exit 2
fi
for tool in shellcheck shfmt; do
  command -v "$tool" >/dev/null || {
    echo "$tool is required for full checks. Install it, or explicitly use --offline." >&2
    exit 1
  }
done
shellcheck -x "${scripts[@]}"
shfmt -d -i 2 "${scripts[@]}"
echo 'Shell lint and formatting passed. Unit syntax validation is a separate Linux CI step.'
