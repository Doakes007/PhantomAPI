from fastapi import FastAPI, Request, Response
import httpx
import time, json
import pandas as pd
from datetime import datetime

LOG_JSON_PATH = "logs/api_logs.json"
LOG_CSV_PATH = "dataset/api_logs.csv"

app = FastAPI()

SERVICE_URL = "https://httpbin.org"

# --- Middleware should be defined BEFORE routes ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()

    # Forward request
    response = await call_next(request)

    latency = round((time.time() - start_time) * 1000, 2)

    # We must NOT await request.body() twice → it gets consumed
    payload_size = len(await request.body()) if request.method in ["POST","PUT","PATCH","DELETE"] else 0

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "method": request.method,
        "endpoint": request.url.path,
        "status": response.status_code,
        "latency_ms": latency,
        "client_ip": request.client.host if request.client else None,
        "payload_size": payload_size
    }

    # Write JSON log
    try:
        with open(LOG_JSON_PATH, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except:
        with open(LOG_JSON_PATH, "w") as f:
            f.write(json.dumps(log_entry) + "\n")

    # Write CSV log
    df = pd.DataFrame([log_entry])
    try:
        df.to_csv(LOG_CSV_PATH, mode="a", index=False, header=False)
    except:
        df.to_csv(LOG_CSV_PATH, index=False)

    print("[LOG]", log_entry)
    return response

# --- Proxy Route ---
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    url = f"{SERVICE_URL}/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("connection", None)

    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream_response = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
            )
    except Exception as e:
        return Response(content=str(e), status_code=502)

    latency = round((upstream_response.elapsed.total_seconds()) * 1000, 2)
    print(f"[PhantomAPI] {request.method} {path} | {latency} ms | Status {upstream_response.status_code}")

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type")
    )
