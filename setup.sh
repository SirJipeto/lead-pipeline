#!/bin/bash
# LeadPipeline Setup (Ubuntu/Debian)
set -e

cd "$(dirname "$0")"

echo "=== LeadPipeline Setup ==="

# Install dependencies into a virtualenv (Ubuntu 24.04 blocks system-wide pip)
echo "[1/3] Installing Python dependencies..."
if [ ! -x .venv/bin/pip ]; then
    rm -rf .venv
    if ! python3 -m venv .venv; then
        rm -rf .venv
        echo ""
        echo "Could not create a virtualenv. Install the missing package, then re-run:"
        echo "  sudo apt update && sudo apt install -y python3-venv"
        exit 1
    fi
fi
.venv/bin/pip install -q -r requirements.txt

# Create .env from example if missing
if [ ! -f .env ]; then
    echo "[2/3] Creating .env from template..."
    cp .env.example .env
    chmod 600 .env
    echo "       → Edit .env and add your API keys"
else
    echo "[2/3] .env already exists, skipping"
fi

# Create data directory
echo "[3/3] Creating data directory..."
mkdir -p data

echo ""
echo "=== Setup Complete ==="
echo ""
echo "1. Edit .env and add your API keys"
echo "2. Run: ./start.sh"
echo "3. Open: http://localhost:5000"
