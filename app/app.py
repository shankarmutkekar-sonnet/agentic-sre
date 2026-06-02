import os
import time
import logging
import json
import ssl
import boto3
import urllib.request
import urllib.error
from flask import Flask, jsonify, request

# ── Splunk HEC Handler ────────────────────────────────────────────────────────


class SplunkHECHandler(logging.Handler):
    def __init__(self, url, token):
        super().__init__()
        self.url = f"{url}/services/collector/event"
        self.token = token
        # Skip SSL verification for Splunk Cloud self-signed cert
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

# BUG (Scenario 2): unbounded in-memory cache — grows forever, never evicted
# In production this looks like a memory leak causing OOM crashes
_item_cache = {}

# ── Scenario 3: Race condition shared state files ─────────────────────────────
# BUG: read-modify-write on SEATS_FILE is unprotected.
# Two Gunicorn workers read the same available-seats list before either
# writes back, causing both to pick the same seat concurrently.
# Detected via append-only JSONL log — duplicate entries reveal the race.
SEATS_FILE    = "/tmp/sre-seats.json"
BOOKINGS_FILE = "/tmp/sre-bookings.jsonl"
TOTAL_SEATS   = 10

def _init_seats():
    seats = {"available": list(range(1, TOTAL_SEATS + 1)), "total": TOTAL_SEATS}
    with open(SEATS_FILE, "w") as f:
        json.dump(seats, f)
    open(BOOKINGS_FILE, "w").close()
    return seats

def _read_seats():
    if not os.path.exists(SEATS_FILE):
        return _init_seats()
    with open(SEATS_FILE) as f:
        return json.load(f)

def _write_seats(state):
    with open(SEATS_FILE, "w") as f:
        json.dump(state, f)

def _append_booking(seat):
    record = json.dumps({"seat": seat, "pid": os.getpid(), "time": time.time()})
    with open(BOOKINGS_FILE, "a") as f:
        f.write(record + "\n")

def _read_bookings():
    if not os.path.exists(BOOKINGS_FILE):
        return []
    bookings = []
    with open(BOOKINGS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    bookings.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return bookings

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

@app.route("/book", methods=["POST"])
def book_seat():
    """
    Scenario 3 — race condition endpoint (seat double-booking).

    Step 1: read available seats from shared file
    Step 2: sleep 50ms — widens race window so both workers reach step 3 together
    Step 3: append booking to JSONL log (both workers append their claim)
    Step 4: last-write-wins update of seats file
    Step 5: re-read JSONL; if our seat appears more than once → race detected
    """
    try:
        state = _read_seats()
        if not state["available"]:
            logger.warning("No seats available — all sold out")
            return jsonify({"error": "sold out"}), 409

        seat = state["available"][0]
        logger.info(f"[pid={os.getpid()}] Attempting to book seat {seat}. Available: {state['available']}")

        time.sleep(0.05)  # BUG: race window — concurrent worker reads same state here

        _append_booking(seat)

        state["available"] = [s for s in state["available"] if s != seat]
        _write_seats(state)

        time.sleep(0.01)  # let concurrent worker finish its append before we check
        all_bookings = _read_bookings()
        seat_claims  = [b for b in all_bookings if b["seat"] == seat]

        if len(seat_claims) > 1:
            pids = [b["pid"] for b in seat_claims]
            logger.error(
                f"CRITICAL: Race condition detected — seat {seat} was claimed "
                f"{len(seat_claims)} times concurrently by PIDs {pids}. "
                f"Root cause: read-modify-write on seat state is not atomic. "
                f"Two Gunicorn workers read the same available-seats list before "
                f"either wrote back, then both appended a booking for the same seat. "
                f"Fix: use a distributed lock (Redis SETNX, DynamoDB conditional write, "
                f"or PostgreSQL SELECT FOR UPDATE) around the read-modify-write sequence.",
                exc_info=False
            )
            emit_error_metric()
            return jsonify({
                "error": "race condition — seat double-booked",
                "seat": seat,
                "claimed_by_pids": pids,
            }), 500

        logger.info(f"[pid={os.getpid()}] Seat {seat} booked. Remaining: {len(state['available'])}")
        return jsonify({"booked": seat, "remaining": len(state["available"])}), 201

    except Exception as exc:
        logger.error(f"/book unexpected error: {exc}", exc_info=True)
        emit_error_metric()
        return jsonify({"error": "internal server error"}), 500


@app.route("/book/reset", methods=["POST"])
def reset_seats():
    """Reset seat state for repeated test runs."""
    _init_seats()
    logger.info("Seat state reset — all seats available again")
    return jsonify({"status": "reset", "seats": TOTAL_SEATS}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)