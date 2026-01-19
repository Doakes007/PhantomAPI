from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
import httpx, time
from prometheus_client import Counter, Histogram, generate_latest

app = FastAPI(title="PhantomAPI Gateway")

SERVICE_URL = "https://httpbin.org"

# ---- Prometheus Metrics ----
REQUEST_COUNT = Counter(
    "api_requests_total",
    "Total requests",
    ["endpoint", "method", "status"]
)

LATENCY = Histogram(
    "api_request_latency_ms",
    "Request latency in ms",
    ["endpoint"]
)

# ---- Health ----
@app.get("/health")
async def health():
    return {"status": "ok"}

# ---- Metrics ----
@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest())

# ---- Proxy ----
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    if path in ["health", "metrics"]:
        return Response(status_code=404)

    url = f"{SERVICE_URL}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    start = time.time()
    resp = await httpx.AsyncClient().request(
        request.method, url, headers=headers, content=body
    )
    latency = (time.time() - start) * 1000

    LATENCY.labels(f"/{path}").observe(latency)
    REQUEST_COUNT.labels(f"/{path}", request.method, resp.status_code).inc()

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type")
    )
