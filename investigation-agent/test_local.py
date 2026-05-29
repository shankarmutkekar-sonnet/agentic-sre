"""
Local test script — Phase 1
Run from inside investigation-agent/:
    python test_local.py

Fires a synthetic Scenario 1 incident payload through the full LangGraph
pipeline and prints the root cause + mitigation plan.
"""

import json
import sys
import os

# Ensure the investigation-agent/ directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from handler import handler

from datetime import datetime, timezone
now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

test_event = {
    "title": "CloudWatch Alarm: flask-api-errors",
    "service": "ec2-flask-api",
    "action": "created",
    "priority": "HIGH",
    "timestamp": now_iso,   # current time so metric/log/commit windows are live
    "description": "Threshold crossed: 15 datapoints > 10 threshold",
    "data": {
        "alarmName": "flask-api-errors",
        "alarmArn": "arn:aws:cloudwatch:eu-north-1:682975283486:alarm:flask-api-errors",
        "accountId": "682975283486",
        "region": "eu-north-1",
        "currentState": "ALARM",
        "previousState": "OK",
        "stateReason": "Threshold crossed: 15 datapoints > 10 threshold"
    }
}

print("=" * 60)
print("Running investigation agent — Scenario 1")
print("=" * 60)

result = handler(test_event, None)
body   = json.loads(result["body"])

print(f"\nStatus     : {result['statusCode']}")
print(f"Processed  : {body['processed']} record(s)\n")

for r in body["results"]:
    print(f"Incident ID  : {r.get('incident_id')}")
    print(f"Status       : {r.get('status')}")
    print(f"\nRoot Cause:\n{r.get('root_cause', 'N/A')}")
    print(f"\n{'=' * 60}")
