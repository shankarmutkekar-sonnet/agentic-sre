#!/bin/bash

PID_FILE=/var/run/flask-app.pid

if [ -f "$PID_FILE" ]; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
fi

# Backup — kill anything on port 5000
fuser -k 5000/tcp 2>/dev/null || true

echo "Flask app stopped"