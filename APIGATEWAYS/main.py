"""API Gateway  (port 8000)
Single entry point. Verifies the JWT, applies role rules, then forwards to the right service.

  /api/<service>/<route inside that service>

Example: GET /api/catalog/products?collection=Winter%202026
"""
import json
import os

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from jose import JWTError, jwt

SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-in-production")  # same as auth service
ALGORITHM = "HS256"

SERVICES = {
    "auth": os.getenv("AUTH_URL", "http://localhost:8001"),
    "catalog": os.getenv("CATALOG_URL", "http://localhost:8002"),
    "customers": os.getenv("CUSTOMER_URL", "http://localhost:8003"),
    "orders": os.getenv("ORDER_URL", "http://localhost:8004"),
    "payments": os.getenv("PAYMENT_URL", "http://localhost:8005"),
    "reports": os.getenv("REPORT_URL", "http://localhost:8006"),
}

# No token needed: sign up / login and browsing the shop.
PUBLIC_RULES = [
    ("POST", "auth", "auth/register"),
    ("POST", "auth", "auth/login"),
    ("GET", "catalog", ""),
]
OWNER_ALL = {"reports"}               # every route owner-only
OWNER_WRITE = {"catalog"}             # only the owner may change the catalog
HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding"}

app = FastAPI(title="Urban Chic Boutique - API Gateway")
client = httpx.AsyncClient(timeout=15)


def is_public(method: str, service: str, path: str) -> bool:
    return any(method == m and service == s and path.startswith(p) for m, s, p in PUBLIC_RULES)


def verify(request: Request) -> dict:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")


def owner_only(method: str, service: str, path: str) -> bool:
    path = path.strip("/")
    if service in OWNER_ALL:
        return True
    if service in OWNER_WRITE and method != "GET":
        return True
    if service == "customers" and method == "GET" and path == "customers":
        return True                                              # list of all customers
    if service == "orders" and method == "PATCH" and path.endswith("/status"):
        return True                                              # shipping / delivery updates
    if service == "payments" and path.endswith("/refund"):
        return True
    return False


@app.get("/health")
async def health():
    """Gateway status plus a ping of every downstream service."""
    status = {}
    for name, base in SERVICES.items():
        try:
            r = await client.get(f"{base}/health", timeout=2)
            status[name] = "ok" if r.status_code == 200 else f"http {r.status_code}"
        except httpx.HTTPError:
            status[name] = "down"
    return {"gateway": "ok", "services": status}


@app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(service: str, path: str, request: Request):
    base = SERVICES.get(service)
    if not base:
        raise HTTPException(404, f"Unknown service '{service}'")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    params = dict(request.query_params)
    body = await request.body()

    if not is_public(request.method, service, path):
        claims = verify(request)
        role, uid = claims.get("role"), str(claims.get("sub"))
        if owner_only(request.method, service, path) and role != "owner":
            raise HTTPException(403, "Boutique owner access required")
        headers["x-user-id"], headers["x-user-role"] = uid, str(role)

        # Customers can only see / create their own orders and payments.
        if role != "owner" and service in {"orders", "payments"}:
            clean = path.strip("/")
            if request.method == "GET" and clean == service:
                params["user_id"] = uid
            if request.method == "POST" and clean == service:
                try:
                    data = json.loads(body or b"{}")
                    data["user_id"] = int(uid)
                    body = json.dumps(data).encode()
                except ValueError:
                    raise HTTPException(422, "Body must be valid JSON")

    try:
        upstream = await client.request(request.method, f"{base}/{path}", params=params,
                                        content=body, headers=headers)
    except httpx.ConnectError:
        raise HTTPException(503, f"{service} service is unavailable")
    except httpx.TimeoutException:
        raise HTTPException(504, f"{service} service timed out")

    out = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP | {"content-encoding"}}
    return Response(upstream.content, status_code=upstream.status_code, headers=out)
