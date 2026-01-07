from fastapi import FastAPI, Request, Response
import httpx, time, json
import pandas as pd
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential
from collections import deque
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST


app = FastAPI()

SERVICE_URL = "https://httpbin.org"
LOG_JSON_PATH = "logs/api_logs.json"
LOG_CSV_PATH = "dataset/api_logs.csv"

# --- Metrics ---
REQUEST_COUNT = Counter("phantomapi_requests_total", "Total API Requests", ["method", "endpoint", "status"])
LATENCY_HIST = Histogram("phantomapi_request_latency_ms", "Latency in ms", ["endpoint"])

# --- Circuit Breaker ---
failure_window = deque(maxlen=20)
circuit_open = False
cb_opened_at = 0
CB_TIMEOUT = 10

@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


def is_circuit_open():
    global circuit_open, cb_opened_at
    if circuit_open and (time.time() - cb_opened_at > CB_TIMEOUT):
        circuit_open = False
    return circuit_open

def record_failure():
    global circuit_open, cb_opened_at
    failure_window.append(1)
    if sum(failure_window) > 10:
        circuit_open = True
        cb_opened_at = time.time()

# --- Retry logic ---
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
async def upstream_call(method, url, headers, body):
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.request(method, url, content=body, headers=headers)

# --- Logging + metrics middleware ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    body = await request.body()
    start = time.time()

    response = await call_next(request)
    latency = round((time.time() - start) * 1000, 2)

    # Record metrics
    REQUEST_COUNT.labels(request.method, request.url.path, response.status_code).inc()
    LATENCY_HIST.labels(request.url.path).observe(latency)

    log = {
        "timestamp": datetime.now().isoformat(),
        "method": request.method,
        "endpoint": request.url.path,
        "status": response.status_code,
        "latency_ms": latency,
        "client_ip": request.client.host if request.client else None,
        "payload_size": len(body)
    }

    # Save logs
    with open(LOG_JSON_PATH, "a") as f:
        f.write(json.dumps(log) + "\n")

    pd.DataFrame([log]).to_csv(LOG_CSV_PATH, mode="a", index=False, header=False)

    print("[LOG]", log)
    return response

# --- Proxy route ---
@app.api_route("/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH"])
async def proxy(path: str, request: Request):
    if is_circuit_open():
        return Response(content="Circuit Breaker is OPEN. Try later.", status_code=503)

    body = await request.body()
    url = f"{SERVICE_URL}/{path}"
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("connection", None)

    try:
        res = await upstream_call(request.method, url, headers, body)
    except Exception as e:
        record_failure()
        return Response(content=str(e), status_code=502)

    # --- anomaly score ---
    score = 0
    if res.status_code >= 500: score += 40
    if res.status_code == 404: score += 20
    if res.elapsed.total_seconds()*1000 > 1500: score += 30
    if len(res.content) > 10_000: score += 10

    anomaly = "ANOMALY" if score > 50 else "NORMAL"
    print(f"[PhantomAPI] {request.method} /{path} | Score: {score} | {anomaly}")

    return Response(
        content=res.content,
        status_code=res.status_code,
        media_type=res.headers.get("content-type")
    )
