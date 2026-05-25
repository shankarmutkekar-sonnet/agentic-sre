import os
import threading
import time

import boto3
from flask import Flask, jsonify, request

app = Flask(__name__)

cloudwatch = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))

_items = [
    {"id": 1, "name": "widget"},
    {"id": 2, "name": "gadget"},
]


def _emit_error_metric():
    try:
        cloudwatch.put_metric_data(
            Namespace="FlaskApp",
            MetricData=[
                {
                    "MetricName": "ErrorCount",
                    "Value": 1,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as exc:
        app.logger.warning("Failed to emit CloudWatch metric: %s", exc)


def _burn_cpu(duration_seconds=5):
    deadline = time.time() + duration_seconds
    while time.time() < deadline:
        pass


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/items", methods=["GET"])
def get_items():
    return jsonify(_items), 200


@app.route("/items", methods=["POST"])
def create_item():
    body = request.get_json(silent=True) or {}
    new_item = {"id": len(_items) + 1, "name": body.get("name", "unnamed")}
    _items.append(new_item)
    return jsonify(new_item), 201


@app.route("/chaos")
def chaos():
    if os.environ.get("CHAOS_MODE") == "1":
        # Burn CPU in a background thread so the response still returns quickly
        threading.Thread(target=_burn_cpu, args=(5,), daemon=True).start()
        _emit_error_metric()
        return jsonify({"error": "chaos mode active"}), 500
    return jsonify({"chaos": "inactive"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
