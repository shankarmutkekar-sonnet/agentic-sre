import os
import time
import logging
import json
import ssl
import boto3
import urllib.request
import urllib.error
import requests as http_requests
from flask import Flask, jsonify, request

# ── Splunk HEC Handler ────────────────────────────────────────────────────────
class SplunkHECHandler(logging.Handler):
    def __init__(self, url, token):
        super().__init__()
        self.url = f"{url}/services/collector/event"
        self.token = token
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    def emit(self, record):
        try:
            payload = json.dumps({
                "event": {
                    "message": self.format(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "function": record.funcName,
                    "line": record.lineno,
                },
                "sourcetype": "flask-api",
                "source": "ec2-flask-app",
                "index": "main"
            }).encode("utf-8")

            req = urllib.request.Request(
                self.url,
                data=payload,
                headers={
                    "Authorization": f"Splunk {self.token}",
                    "Content-Type": "application/json"
                }
            )
            urllib.request.urlopen(req, timeout=2, context=self.ssl_context)
        except Exception:
            pass

# ── Logging setup ─────────────────────────────────────────────────────────────
SPLUNK_URL   = os.environ.get("SPLUNK_HEC_URL", "")
SPLUNK_TOKEN = os.environ.get("SPLUNK_HEC_TOKEN", "")

handlers = [
    logging.FileHandler('/var/log/flask-app/app.log'),
    logging.StreamHandler()
]

if SPLUNK_URL and SPLUNK_TOKEN:
    handlers.append(SplunkHECHandler(SPLUNK_URL, SPLUNK_TOKEN))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)

if SPLUNK_URL and SPLUNK_TOKEN:
    logger.info("Splunk HEC logging enabled")
else:
    logger.warning("Splunk HEC not configured - logging to file only")

# ── App setup ─────────────────────────────────────────────────────────────────
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

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    logger.info("Health check requested")
    return jsonify({"status": "healthy"}), 200

@app.route("/items", methods=["GET"])
def get_items():
    try:
        logger.info("GET /items - fetching from inventory service")
        # Calls external inventory service with impossibly short timeout
        response = http_requests.get(
            "http://inventory-service.internal/items",
            timeout=0.001
        )
        return jsonify({"items": response.json()}), 200
    except http_requests.exceptions.Timeout:
        logger.error(
            "GET /items failed - inventory-service timed out after 0.001s. "
            "Service may be overloaded or unreachable. "
            "Host: inventory-service.internal, timeout=0.001s",
            exc_info=True
        )
        emit_error_metric()
        return jsonify({"error": "upstream service timeout"}), 500
    except http_requests.exceptions.ConnectionError:
        logger.error(
            "GET /items failed - cannot connect to inventory-service.internal. "
            "Service is unreachable or DNS resolution failed.",
            exc_info=True
        )
        emit_error_metric()
        return jsonify({"error": "upstream service unreachable"}), 500
    except Exception as e:
        logger.error(f"GET /items unexpected error: {str(e)}", exc_info=True)
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
        logger.error("CHAOS_MODE=1 active - returning 500 error intentionally")
        emit_error_metric()
        end = time.time() + 0.5
        while time.time() < end:
            pass
        return jsonify({"error": "chaos mode active"}), 500
    logger.info("GET /chaos requested - normal mode")
    return jsonify({"status": "all good"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)