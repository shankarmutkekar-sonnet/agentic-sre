#!/bin/bash
set -e

APP_DIR=/home/ec2-user
LOG_DIR=/var/log/flask-app
PID_FILE=/home/ec2-user/flask-app.pid
GUNICORN=/home/ec2-user/.local/bin/gunicorn

sudo mkdir -p $LOG_DIR
sudo chown ec2-user:ec2-user $LOG_DIR
fuser -k 5000/tcp 2>/dev/null || true
sleep 1

# Load env vars
source /home/ec2-user/.bashrc

$GUNICORN \
  --workers 2 \
  --bind 0.0.0.0:5000 \
  --daemon \
  --pid "$PID_FILE" \
  --log-file "$LOG_DIR/gunicorn.log" \
  --chdir "$APP_DIR" \
  --env SPLUNK_HEC_URL=$SPLUNK_HEC_URL \
  --env SPLUNK_HEC_TOKEN=$SPLUNK_HEC_TOKEN \
  app:app

echo "Flask app started successfully"