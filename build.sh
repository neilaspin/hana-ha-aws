#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_DIR="$SCRIPT_DIR/lambda/package"

echo "Building Lambda package..."
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

cp "$SCRIPT_DIR/lambda/failover.py" "$PACKAGE_DIR/"

pip install paramiko \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: \
  --target "$PACKAGE_DIR/" \
  -q

echo "Lambda package ready."
