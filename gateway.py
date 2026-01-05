from fastapi import FastAPI, Request, Response
import httpx
import time

app = FastAPI()

SERVICE_URL = "https://httpbin.org"

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    url = f"{SERVICE_URL}/{path}"

    # Clean headers (remove Host & Connection headers)
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("connection", None)

    body = await request.body()

    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream_response = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
            )
    except Exception as e:
        return Response(
            content=f"Upstream request failed: {str(e)}",
            status_code=502
        )

    latency = (time.time() - start_time) * 1000  # ms

    # Log latency in console (dataset use later)
    print(f"[PhantomAPI] {request.method} {path} | {latency:.2f} ms | Status {upstream_response.status_code}")

    # Return raw response safely
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type")
    )
