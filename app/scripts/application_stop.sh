#!/bin/bash

PID_FILE=/var/run/flask-app.pid

if [ -f "$PID_FILE" ]; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
fi
