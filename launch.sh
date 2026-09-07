#!/usr/bin/env bash

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$APP_DIR"

exec "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py"