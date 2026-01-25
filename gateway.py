from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
import httpx
import time
import asyncio
from collections import deque
from prometheus_client import Counter, Histogram, Gauge, generate_latest

app = FastAPI(title="PhantomAPI Gateway")

# -------------------- Config --------------------
SERVICE_URL = "https://httpbin.org"
UPSTREAM_TIMEOUT_SECONDS = 2.0

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.2
IDEMPOTENT_METHODS = {"GET", "HEAD"}

# -------------------- Circuit Breaker Config --------------------
CIRCUIT_WINDOW_SIZE = 20
CIRCUIT_FAILURE_THRESHOLD = 0.5
CIRCUIT_MIN_REQUESTS = 10
CIRCUIT_OPEN_DURATION_SECONDS = 30

failure_window = deque(maxlen=CIRCUIT_WINDOW_SIZE)

circuit_state = "CLOSED"   # CLOSED | OPEN | HALF_OPEN
circuit_opened_at = None
half_open_probe_in_flight = False

# -------------------- Metrics --------------------
REQUEST_COUNT = Counter(
    "api_requests_total",
    "Total API requests",
    ["endpoint", "method", "status"]
)

LATENCY = Histogram(
    "api_request_latency_ms",
    "API request latency",
    ["endpoint"]
)

UPSTREAM_TIMEOUTS = Counter(
    "upstream_timeouts_total",
    "Upstream timeouts",
    ["endpoint", "method"]
)

UPSTREAM_5XX_ERRORS = Counter(
    "upstream_5xx_errors_total",
    "Upstream 5xx errors",
    ["endpoint", "method"]
)

UPSTREAM_RETRIES = Counter(
    "upstream_retries_total",
    "Upstream retries",
    ["endpoint", "method"]
)

UPSTREAM_RETRY_EXHAUSTED = Counter(
    "upstream_retry_exhausted_total",
    "Retries exhausted",
    ["endpoint", "method"]
)

CIRCUIT_FAILURE_RATIO = Histogram(
    "circuit_failure_ratio",
    "Circuit failure ratio"
)

CIRCUIT_REQUESTS_TRACKED = Counter(
    "circuit_requests_tracked_total",
    "Requests tracked"
)

CIRCUIT_STATE = Gauge(
    "circuit_state",
    "Circuit state (0=closed,1=open,2=half_open)"
)

CIRCUIT_OPEN_TOTAL = Counter(
    "circuit_open_total",
    "Circuit opened count"
)

CIRCUIT_SHORT_CIRCUITED = Counter(
    "circuit_short_circuited_total",
    "Short-circuited requests"
)

CIRCUIT_STATE.set(0)

# -------------------- Circuit Helper --------------------
def maybe_open_circuit():
    global circuit_state, circuit_opened_at, half_open_probe_in_flight

    if len(failure_window) >= CIRCUIT_MIN_REQUESTS:
        ratio = sum(failure_window) / len(failure_window)
        if ratio >= CIRCUIT_FAILURE_THRESHOLD and circuit_state == "CLOSED":
            circuit_state = "OPEN"
            circuit_opened_at = time.time()
            half_open_probe_in_flight = False
            CIRCUIT_STATE.set(1)
            CIRCUIT_OPEN_TOTAL.inc()

# -------------------- Endpoints --------------------
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest())

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
async def proxy(path: str, request: Request):
    global circuit_state, circuit_opened_at, half_open_probe_in_flight

    if path in ["health", "metrics"]:
        return Response(status_code=404)

    now = time.time()

    # -------- Circuit State Handling --------
    if circuit_state == "OPEN":
        if now - circuit_opened_at >= CIRCUIT_OPEN_DURATION_SECONDS:
            circuit_state = "HALF_OPEN"
            CIRCUIT_STATE.set(2)
        else:
            CIRCUIT_SHORT_CIRCUITED.inc()
            REQUEST_COUNT.labels(f"/{path}", request.method, 503).inc()
            return Response(b"Circuit open", status_code=503)

    if circuit_state == "HALF_OPEN":
        if half_open_probe_in_flight:
            CIRCUIT_SHORT_CIRCUITED.inc()
            REQUEST_COUNT.labels(f"/{path}", request.method, 503).inc()
            return Response(b"Half-open probe in progress", status_code=503)
        half_open_probe_in_flight = True

    url = f"{SERVICE_URL}/{path}"
    method = request.method
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    attempts = 0
    start = time.time()

    while True:
        try:
            async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
                resp = await client.request(method, url, headers=headers, content=body)

            LATENCY.labels(f"/{path}").observe((time.time() - start) * 1000)
            REQUEST_COUNT.labels(f"/{path}", method, resp.status_code).inc()

            is_failure = 500 <= resp.status_code < 600

            failure_window.append(is_failure)
            CIRCUIT_REQUESTS_TRACKED.inc()

            # ----- HALF_OPEN success -----
            if circuit_state == "HALF_OPEN" and not is_failure:
                circuit_state = "CLOSED"
                half_open_probe_in_flight = False
                circuit_opened_at = None
                failure_window.clear()
                CIRCUIT_STATE.set(0)

            if failure_window:
                ratio = sum(failure_window) / len(failure_window)
                CIRCUIT_FAILURE_RATIO.observe(ratio)

            if is_failure:
                UPSTREAM_5XX_ERRORS.labels(f"/{path}", method).inc()
                maybe_open_circuit()

                if method in IDEMPOTENT_METHODS and attempts < MAX_RETRIES:
                    attempts += 1
                    UPSTREAM_RETRIES.labels(f"/{path}", method).inc()
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempts)
                    continue

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type")
            )

        except httpx.TimeoutException:
            UPSTREAM_TIMEOUTS.labels(f"/{path}", method).inc()

            failure_window.append(True)
            CIRCUIT_REQUESTS_TRACKED.inc()

            if failure_window:
                ratio = sum(failure_window) / len(failure_window)
                CIRCUIT_FAILURE_RATIO.observe(ratio)

            maybe_open_circuit()

            if method in IDEMPOTENT_METHODS and attempts < MAX_RETRIES:
                attempts += 1
                UPSTREAM_RETRIES.labels(f"/{path}", method).inc()
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempts)
                continue

            UPSTREAM_RETRY_EXHAUSTED.labels(f"/{path}", method).inc()

            if circuit_state == "HALF_OPEN":
                circuit_state = "OPEN"
                half_open_probe_in_flight = False
                circuit_opened_at = time.time()
                CIRCUIT_STATE.set(1)
                CIRCUIT_OPEN_TOTAL.inc()

            REQUEST_COUNT.labels(f"/{path}", method, 504).inc()
            return Response(b"Upstream timeout", status_code=504)
