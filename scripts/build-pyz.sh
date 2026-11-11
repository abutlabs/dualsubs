#!/bin/sh
# Build dist/dualsubs: a single-file, dependency-free executable (Python zipapp).
# It runs anywhere with Python 3.9+:  ./dualsubs --version
set -eu
cd "$(dirname "$0")/.."
rm -rf build/pyz && mkdir -p build/pyz dist
cp -r dualsubs build/pyz/
find build/pyz -name __pycache__ -prune -exec rm -rf {} +
python3 -m zipapp build/pyz -m "dualsubs.cli:run" -p "/usr/bin/env python3" -c -o dist/dualsubs
chmod +x dist/dualsubs
rm -rf build/pyz
echo "built dist/dualsubs ($(wc -c < dist/dualsubs) bytes), version $(python3 dist/dualsubs --version)"
