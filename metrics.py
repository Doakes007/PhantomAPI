from fastapi import FastAPI, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

REQUEST_COUNT = Counter("phantomapi_requests_total", "Total API Requests", ["method", "endpoint", "status"])
REQUEST_LATENCY = Histogram("phantomapi_request_latency_ms", "Request latency in ms", ["endpoint"])

def setup_metrics(app: FastAPI):
    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
