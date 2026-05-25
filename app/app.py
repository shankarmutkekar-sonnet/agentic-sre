import os
import time
import logging
import boto3
from flask import Flask, jsonify, request

# Configure logging to write to a file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/var/log/flask-app/app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
cw = boto3.client("cloudwatch", region_name="eu-north-1")

def emit_error_metric():
    cw.put_metric_data(
        Namespace="FlaskApp",
        MetricData=[{
            "MetricName": "ErrorCount",
            "Value": 1,
            "Unit": "Count"
        }]
    )

@app.route("/health")
def health():
    logger.info("Health check requested")
    return jsonify({"status": "healthy"}), 200

@app.route("/items", methods=["GET"])
def get_items():
    try:
        # BUG: divide by zero introduced accidentally
        result = 1 / 0
        return jsonify({"items": result}), 200
    except Exception as e:
        logger.error(f"GET /items failed with error: {str(e)}", exc_info=True)
        emit_error_metric()
        return jsonify({"error": "internal server error"}), 500

@app.route("/items", methods=["POST"])
def post_item():
    data = request.get_json()
    logger.info(f"POST /items requested with data: {data}")
    return jsonify({"created": data}), 201

@app.route("/chaos")
def chaos():
    if os.environ.get("CHAOS_MODE") == "1":
        logger.error(f"CHAOS_MODE=1 active - returning 500 error intentionally")
        emit_error_metric()
        end = time.time() + 0.5
        while time.time() < end:
            pass
        return jsonify({"error": "chaos mode active"}), 500
    logger.info("GET /chaos requested - normal mode")
    return jsonify({"status": "all good"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)