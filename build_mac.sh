#!/bin/bash
set -euo pipefail

missing=0
packages=()
for module in PyInstaller bleak webview; do
  python3 -c "import importlib.util,sys;mod='${module}';sys.exit(0 if importlib.util.find_spec(mod) else 1)"
  if [ $? -ne 0 ]; then
    echo "Missing dependency: ${module}" >&2
    missing=1
    if [ "${module}" = "webview" ]; then
      packages+=("pywebview")
    else
      packages+=("${module}")
    fi
  fi
done

if [ $missing -ne 0 ]; then
  read -r -p "Install missing dependencies now? (y/n): " reply
  if [ "${reply}" = "y" ] || [ "${reply}" = "Y" ]; then
    python3 -m pip install "${packages[@]}"
  else
    echo "Install missing dependencies and rerun." >&2
    exit 1
  fi
fi

python3 -m PyInstaller "Bobs Humidity Tracker.spec"
