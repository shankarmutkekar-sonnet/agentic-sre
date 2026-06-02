import os
import time
import json
import logging
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

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
cw  = boto3.client("cloudwatch", region_name="eu-north-1")

# ── Shared state files (simulate an external cache / DB both workers hit) ─────
#
# BUG: all read-modify-write operations on SEATS_FILE are unprotected.
# Two Gunicorn workers can interleave their reads and writes:
#
#   T=0ms   Worker A reads seats_file  →  available=[1,2,…,10]
#   T=0ms   Worker B reads seats_file  →  available=[1,2,…,10]
#   T=0ms   Worker A picks seat 1, Worker B picks seat 1  ← SAME SEAT
#   T=50ms  Worker A appends booking {seat:1, pid:A} to bookings log
#   T=50ms  Worker B appends booking {seat:1, pid:B} to bookings log
#   T=50ms  Worker A writes seats_file: available=[2,…,10]
#   T=50ms  Worker B writes seats_file: available=[2,…,10]  ← overwrites A
#
# Outcome: seat 1 is double-booked (both workers successfully "claimed" it).
# The JSONL bookings log — which is append-only — captures both writes and
# makes the duplicate detectable.

SEATS_FILE    = "/tmp/sre-seats.json"
BOOKINGS_FILE = "/tmp/sre-bookings.jsonl"
TOTAL_SEATS   = 10


def _emit_error_metric():
    cw.put_metric_data(
        Namespace="FlaskApp",
        MetricData=[{"MetricName": "ErrorCount", "Value": 1, "Unit": "Count"}]
    )


def _init_seats():
    """Create fresh seat state. Called on /book/reset and on first use."""
    seats = {"available": list(range(1, TOTAL_SEATS + 1)), "total": TOTAL_SEATS}
    with open(SEATS_FILE, "w") as f:
        json.dump(seats, f)
    open(BOOKINGS_FILE, "w").close()  # truncate booking log
    return seats


def _read_seats() -> dict:
    if not os.path.exists(SEATS_FILE):
        return _init_seats()
    with open(SEATS_FILE) as f:
        return json.load(f)


def _write_seats(state: dict):
    with open(SEATS_FILE, "w") as f:
        json.dump(state, f)


def _append_booking(seat: int):
    """Append a booking record to the JSONL log. Append is safe under concurrency."""
    record = json.dumps({
        "seat": seat,
        "pid":  os.getpid(),
        "time": time.time(),
    })
    with open(BOOKINGS_FILE, "a") as f:
        f.write(record + "\n")


def _read_bookings() -> list[dict]:
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    logger.info("Health check requested")
    return jsonify({"status": "healthy"}), 200


@app.route("/book", methods=["POST"])
def book_seat():
    """
    Race-condition endpoint.

    Step 1 — read available seats from shared file
    Step 2 — sleep 50 ms (widens the race window so both workers reach step 3 together)
    Step 3 — append booking to JSONL log (both workers append their claim)
    Step 4 — last-write-wins update of the seats file
    Step 5 — re-read the JSONL log; if our seat appears more than once → race detected
    """
    try:
        # ── Step 1: read ─────────────────────────────────────────────────────
        state = _read_seats()
        if not state["available"]:
            logger.warning("No seats available — all sold out")
            return jsonify({"error": "sold out"}), 409

        seat = state["available"][0]
        logger.info(
            f"[pid={os.getpid()}] Attempting to book seat {seat}. "
            f"Available: {state['available']}"
        )

        # ── Step 2: race window ───────────────────────────────────────────────
        # A concurrent worker reads the same state during this sleep.
        time.sleep(0.05)

        # ── Step 3: claim seat in append-only log ─────────────────────────────
        _append_booking(seat)

        # ── Step 4: update shared seats file (no lock → last-write-wins) ──────
        state["available"] = [s for s in state["available"] if s != seat]
        _write_seats(state)

        # ── Step 5: brief settle window, then integrity check ─────────────────
        # Give the other worker time to finish its append before we read the log.
        time.sleep(0.01)
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
            _emit_error_metric()
            return jsonify({
                "error":        "race condition — seat double-booked",
                "seat":         seat,
                "claimed_by_pids": pids,
            }), 500

        logger.info(
            f"[pid={os.getpid()}] Seat {seat} booked successfully. "
            f"Remaining: {len(state['available'])}"
        )
        return jsonify({"booked": seat, "remaining": len(state["available"])}), 201

    except Exception as exc:
        logger.error(f"/book unexpected error: {exc}", exc_info=True)
        _emit_error_metric()
        return jsonify({"error": "internal server error"}), 500


@app.route("/book/reset", methods=["POST"])
def reset_seats():
    """Reset seat state for repeated test runs. Does not affect the alarm."""
    _init_seats()
    logger.info("Seat state reset — all seats available again")
    return jsonify({"status": "reset", "seats": TOTAL_SEATS}), 200


@app.route("/items", methods=["GET"])
def get_items():
    logger.info("GET /items requested")
    return jsonify({"items": ["item-1", "item-2", "item-3"]}), 200


@app.route("/items", methods=["POST"])
def post_item():
    data = request.get_json()
    logger.info(f"POST /items requested with data: {data}")
    return jsonify({"created": data}), 201


@app.route("/chaos")
def chaos():
    if os.environ.get("CHAOS_MODE") == "1":
        logger.error("CHAOS_MODE=1 active - returning 500 error intentionally")
        _emit_error_metric()
        end = time.time() + 0.5
        while time.time() < end:
            pass
        return jsonify({"error": "chaos mode active"}), 500
    logger.info("GET /chaos requested - normal mode")
    return jsonify({"status": "all good"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
