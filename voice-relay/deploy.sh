#!/bin/bash
set -e

APP_DIR=/opt/voice-relay
SERVICE_FILE=/etc/systemd/system/voice-relay.service

echo "==> Copying files to $APP_DIR"
sudo mkdir -p $APP_DIR
sudo cp -r app requirements.txt $APP_DIR/

if [ ! -f "$APP_DIR/.env" ]; then
    sudo cp .env.example $APP_DIR/.env
    echo "==> Created .env from template, please edit $APP_DIR/.env"
fi

echo "==> Setting up venv"
if [ ! -d "$APP_DIR/venv" ]; then
    sudo python3 -m venv $APP_DIR/venv
fi
sudo $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt -q

echo "==> Installing systemd service"
sudo cp systemd/voice-relay.service $SERVICE_FILE
sudo systemctl daemon-reload
sudo systemctl enable voice-relay
sudo systemctl restart voice-relay

echo "==> Done. Status:"
sudo systemctl status voice-relay --no-pager
