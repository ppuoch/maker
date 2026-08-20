#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -U pip
  pip install "git+https://github.com/Loaf-Markets/loaf-python-api-bot-template.git"
  pip install -e .
else
  source .venv/bin/activate
fi
exec python -m loaf_bot.main
