#!/bin/bash
set -e

APP_DIR=/opt/flask-app
PID_FILE=/var/run/flask-app.pid
LOG_FILE=/var/log/flask-app.log

# Start gunicorn with 2 workers; bind to all interfaces on port 5000
gunicorn \
  --workers 2 \
  --bind 0.0.0.0:5000 \
  --daemon \
  --pid "$PID_FILE" \
  --log-file "$LOG_FILE" \
  --chdir "$APP_DIR" \
  app:app
