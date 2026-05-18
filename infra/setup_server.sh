#!/bin/bash
# Run once on infograph server to install all dependencies
set -e

echo "=== Figure AI Tracker — Server Setup ==="

# System deps
sudo apt-get update -q
sudo apt-get install -y -q \
  python3.11 python3.11-venv python3-pip \
  tesseract-ocr tesseract-ocr-eng \
  ffmpeg \
  nginx \
  libgl1

# Create app directories
sudo mkdir -p /opt/figure-tracker/{backend,frontend,data}
sudo chown -R ubuntu:ubuntu /opt/figure-tracker

# Python virtualenv
python3.11 -m venv /opt/figure-tracker/venv
/opt/figure-tracker/venv/bin/pip install --upgrade pip -q
/opt/figure-tracker/venv/bin/pip install -r /opt/figure-tracker/backend/requirements.txt -q

echo "=== Dependencies installed ==="

# Nginx config
sudo cp /opt/figure-tracker/infra/nginx.conf /etc/nginx/sites-available/figure-tracker
sudo ln -sf /etc/nginx/sites-available/figure-tracker /etc/nginx/sites-enabled/figure-tracker
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

echo "=== Nginx configured ==="

# Systemd service
sudo cp /opt/figure-tracker/infra/figure-tracker-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable figure-tracker-api
sudo systemctl start figure-tracker-api

echo "=== Service started ==="
echo ""
echo "Seed historical data:"
echo "  /opt/figure-tracker/venv/bin/python /opt/figure-tracker/backend/seed_data.py"
echo ""
echo "View logs:"
echo "  journalctl -u figure-tracker-api -f"
echo ""
echo "Done! Site running at http://$(curl -s ifconfig.me)"
