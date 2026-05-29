import os
import time
import logging
import boto3
from flask import Flask, jsonify, request

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

# BUG: unbounded in-memory cache — grows forever, never evicted
# In production this looks like a memory leak causing OOM crashes
_item_cache = {}

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
        logger.info(f"GET /items - cache size: {len(_item_cache)} entries")
        # Simulate cache growing unboundedly
        cache_key = f"items_{time.time()}"
        _item_cache[cache_key] = ["item-1", "item-2", "item-3"] * 1000

        if len(_item_cache) > 100:
            logger.error(
                f"CRITICAL: Item cache has grown to {len(_item_cache)} entries. "
                f"Estimated memory usage: {len(_item_cache) * 24}KB. "
                "Cache eviction policy missing — possible memory leak.",
                exc_info=False
            )
            emit_error_metric()
            raise MemoryError(
                f"Item cache exceeded safe limit: {len(_item_cache)} entries"
            )

        return jsonify({"items": _item_cache[cache_key]}), 200
    except MemoryError as e:
        logger.error(f"GET /items failed - MemoryError: {str(e)}", exc_info=True)
        emit_error_metric()
        return jsonify({"error": "service unavailable - memory limit exceeded"}), 500
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