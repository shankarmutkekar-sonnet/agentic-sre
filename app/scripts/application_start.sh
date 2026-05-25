#!/bin/bash
set -e

APP_DIR=/home/ec2-user
LOG_DIR=/var/log/flask-app
PID_FILE=/home/ec2-user/flask-app.pid
GUNICORN=/home/ec2-user/.local/bin/gunicorn

# Create log directory if it doesn't exist
sudo mkdir -p $LOG_DIR
sudo chown ec2-user:ec2-user $LOG_DIR

# Kill any existing Flask process on port 5000
fuser -k 5000/tcp 2>/dev/null || true
sleep 1

# Start gunicorn using full path
$GUNICORN \
  --workers 2 \
  --bind 0.0.0.0:5000 \
  --daemon \
  --pid "$PID_FILE" \
  --log-file "$LOG_DIR/gunicorn.log" \
  --chdir "$APP_DIR" \
  app:app

echo "Flask app started successfully"