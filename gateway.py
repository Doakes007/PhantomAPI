from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
import httpx
import time
import asyncio
from collections import deque
from prometheus_client import Counter, Histogram, Gauge, generate_latest

from features.extractor import FeatureExtractor
from features.logger import FeatureLogger
from features.predictor import FailureRiskPredictor
from features.adaptive import AdaptiveThresholdController


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

# -------------------- Predictive Config --------------------
PREDICTIVE_CHECK_INTERVAL = 5
PREDICTIVE_COOLDOWN = 30
last_predictive_action = 0

# -------------------- Phase 3.5 Soft Mitigation --------------------
SOFT_RISK_THRESHOLD = 0.45
HARD_RISK_THRESHOLD = 0.7

DEGRADED_TIMEOUT_SECONDS = 1.0
DEGRADED_MAX_RETRIES = 0

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

# -------------------- Feature Extractor --------------------
feature_extractor = FeatureExtractor(
    request_total=REQUEST_COUNT,
    request_failures=UPSTREAM_5XX_ERRORS,
    request_timeouts=UPSTREAM_TIMEOUTS,
    request_retries=UPSTREAM_RETRIES,
    circuit_transitions=CIRCUIT_OPEN_TOTAL,
    latency_histogram=LATENCY,
)

# -------------------- Feature Logger --------------------
feature_logger = FeatureLogger(
    extractor=feature_extractor,
    output_path="dataset/phase3_features.csv",
)

# -------------------- ML + Adaptive Threshold --------------------
risk_predictor = FailureRiskPredictor(
    model_path="experiments/models/failure_risk_model.joblib"
)
adaptive_controller = AdaptiveThresholdController()

# -------------------- Startup --------------------
@app.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(feature_extractor.start())
    asyncio.create_task(feature_logger.start())
    asyncio.create_task(predictive_circuit_controller())

# -------------------- Predictive Circuit Controller --------------------
async def predictive_circuit_controller():
    global circuit_state, circuit_opened_at, last_predictive_action

    while True:
        await asyncio.sleep(PREDICTIVE_CHECK_INTERVAL)

        if circuit_state != "CLOSED":
            continue

        now = time.time()
        if now - last_predictive_action < PREDICTIVE_COOLDOWN:
            continue

        features = feature_extractor.compute_features()
        risk = risk_predictor.predict_risk(features)
        threshold = adaptive_controller.compute_threshold(features)

        if risk >= threshold:
            circuit_state = "OPEN"
            circuit_opened_at = time.time()
            CIRCUIT_STATE.set(1)
            CIRCUIT_OPEN_TOTAL.inc()
            last_predictive_action = now

# -------------------- Helpers --------------------
def request_mode_from_risk(risk: float):
    if risk >= HARD_RISK_THRESHOLD:
        return "HARD_FAIL"
    elif risk >= SOFT_RISK_THRESHOLD:
        return "DEGRADED"
    return "NORMAL"


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

@app.get("/debug/features")
async def debug_features():
    return feature_extractor.compute_features()

@app.get("/debug/risk")
async def debug_risk():
    features = feature_extractor.compute_features()
    risk = risk_predictor.predict_risk(features)
    return {
        "risk": risk,
        "adaptive_threshold": adaptive_controller.compute_threshold(features),
        "features": features,
    }

@app.get("/debug/mode")
async def debug_mode():
    features = feature_extractor.compute_features()
    risk = risk_predictor.predict_risk(features)
    return {
        "risk": risk,
        "mode": request_mode_from_risk(risk),
        "features": features,
    }

# -------------------- Proxy --------------------
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
async def proxy(path: str, request: Request):
    global circuit_state, circuit_opened_at, half_open_probe_in_flight

    if path in ["health", "metrics", "debug"]:
        return Response(status_code=404)

    now = time.time()

    # ----- Circuit enforcement -----
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
            return Response(b"Half-open probe in progress", status_code=503)
        half_open_probe_in_flight = True

    # ----- Phase 3.5 Risk-based behavior -----
    features = feature_extractor.compute_features()
    risk = risk_predictor.predict_risk(features)
    mode = request_mode_from_risk(risk)

    if mode == "HARD_FAIL":
        CIRCUIT_SHORT_CIRCUITED.inc()
        return Response(b"Service temporarily degraded", status_code=429)

    timeout = UPSTREAM_TIMEOUT_SECONDS
    max_retries = MAX_RETRIES

    if mode == "DEGRADED":
        timeout = DEGRADED_TIMEOUT_SECONDS
        max_retries = DEGRADED_MAX_RETRIES

    url = f"{SERVICE_URL}/{path}"
    method = request.method
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    attempts = 0
    start = time.time()

    while True:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers, content=body)

            LATENCY.labels(f"/{path}").observe((time.time() - start) * 1000)
            REQUEST_COUNT.labels(f"/{path}", method, resp.status_code).inc()

            is_failure = 500 <= resp.status_code < 600
            failure_window.append(is_failure)
            CIRCUIT_REQUESTS_TRACKED.inc()

            if circuit_state == "HALF_OPEN" and not is_failure:
                circuit_state = "CLOSED"
                half_open_probe_in_flight = False
                circuit_opened_at = None
                failure_window.clear()
                CIRCUIT_STATE.set(0)

            if is_failure:
                UPSTREAM_5XX_ERRORS.labels(f"/{path}", method).inc()
                maybe_open_circuit()

                if method in IDEMPOTENT_METHODS and attempts < max_retries:
                    attempts += 1
                    UPSTREAM_RETRIES.labels(f"/{path}", method).inc()
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempts)
                    continue

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type"),
            )

        except httpx.TimeoutException:
            UPSTREAM_TIMEOUTS.labels(f"/{path}", method).inc()
            failure_window.append(True)
            CIRCUIT_REQUESTS_TRACKED.inc()
            maybe_open_circuit()

            if method in IDEMPOTENT_METHODS and attempts < max_retries:
                attempts += 1
                UPSTREAM_RETRIES.labels(f"/{path}", method).inc()
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempts)
                continue

            UPSTREAM_RETRY_EXHAUSTED.labels(f"/{path}", method).inc()
            return Response(b"Upstream timeout", status_code=504)
