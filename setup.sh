#!/bin/bash
# LeadPipeline Setup
set -e

cd "$(dirname "$0")"

echo "=== LeadPipeline Setup ==="

# Install dependencies
echo "[1/3] Installing Python dependencies..."
pip3 install --break-system-packages -q -r requirements.txt 2>/dev/null || pip3 install -q -r requirements.txt

# Create .env from example if missing
if [ ! -f .env ]; then
    echo "[2/3] Creating .env from template..."
    cp .env.example .env
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
echo "2. Run: python app.py"
echo "3. Open: http://localhost:5000"
