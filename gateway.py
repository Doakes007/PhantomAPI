from fastapi import FastAPI, Request, Response
import httpx, time, json
import pandas as pd
from datetime import datetime
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# --- Add Rate Limiting Imports ---
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

app = FastAPI()

# --- Prometheus Metrics ---
REQUEST_COUNT = Counter(
    "phantomapi_requests_total",
    "Total API Requests",
    ["method", "endpoint", "status"]
)

LATENCY_HIST = Histogram(
    "phantomapi_request_latency_ms",
    "Latency in ms",
    ["endpoint"]
)

# --- Initialize Rate Limiter ---
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(429, _rate_limit_exceeded_handler)

# --- Expose metrics endpoint BEFORE proxy ---
@app.get("/metrics")
def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )

# --- Proxy Route (resilient gateway + rate limited) ---
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@limiter.limit("5/minute")  # 5 requests/min per client IP
async def proxy(path: str, request: Request):

    url = f"https://httpbin.org/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("connection", None)

    body = await request.body()

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.request(request.method, url, content=body, headers=headers)
    except Exception as e:
        return Response(content=str(e), status_code=502)

    latency = round((time.time() - start) * 1000, 2)

    # Record metrics
    REQUEST_COUNT.labels(request.method, request.url.path, res.status_code).inc()
    LATENCY_HIST.labels(request.url.path).observe(latency)

    print(f"[{datetime.now().isoformat()}] {request.method} {request.url.path} | {latency}ms | {res.status_code}")

    return Response(
        content=res.content,
        status_code=res.status_code,
        media_type=res.headers.get("content-type")
    )
