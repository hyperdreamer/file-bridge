#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "$0")"

if [[ ! -x .venv/bin/python ]]; then
    echo "ERROR: .venv/bin/python is missing or not executable." >&2
    echo "Create the environment and install requirements.txt first." >&2
    exit 1
fi

if [[ ! -f config.yaml ]]; then
    echo "ERROR: config.yaml is missing." >&2
    echo "Copy config.example.yaml to config.yaml and set save_root." >&2
    exit 1
fi

if [[ ! -r config.yaml ]]; then
    echo "ERROR: config.yaml is not readable." >&2
    exit 1
fi

if ! .venv/bin/python main.py --check-config; then
    echo "ERROR: startup checks failed; fix config.yaml/save_root and retry." >&2
    exit 1
fi

exec .venv/bin/python main.py
