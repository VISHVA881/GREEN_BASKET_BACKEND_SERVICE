"""Report Service  (port 8006)
Read-only analytics. No database of its own: it aggregates data from the
Order, Inventory, Product Listing and Customer services over HTTP."""
import os
from collections import Counter, defaultdict
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException

ORDER_URL = os.getenv("ORDER_URL", "http://localhost:8004")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8003")
LISTINGS_URL = os.getenv("LISTINGS_URL", "http://localhost:8002")
CUSTOMER_URL = os.getenv("CUSTOMER_URL", "http://localhost:8005")

app = FastAPI(title="Green Basket Organic Store - Report Service")


def fetch(base: str, path: str, params: Optional[dict] = None):
    try:
        r = httpx.get(f"{base}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Upstream service error ({base}): {e}")


def _orders() -> list[dict]:
    return fetch(ORDER_URL, "/orders", {"limit": 1000})


def _live(orders: list[dict]) -> list[dict]:
    return [o for o in orders if o["status"] != "cancelled"]


def _names() -> dict[int, str]:
    prods = fetch(LISTINGS_URL, "/products", {"limit": 200, "active_only": False})
    return {p["id"]: p["name"] for p in prods}


@app.get("/health")
def health():
    return {"service": "report", "status": "ok"}


@app.get("/reports/sales")
def sales_report():
    orders = _orders()
    live = _live(orders)
    paid = [o for o in live if o["payment_status"] == "paid"]
    unpaid = [o for o in live if o["payment_status"] == "unpaid"]
    by_month = defaultdict(float)
    for o in paid:
        by_month[o["created_at"][:7]] += o["total_amount"]
    by_status = Counter(o["status"] for o in orders)
    total = len(orders)
    return {
        "total_orders": total,
        "orders_by_status": dict(by_status),
        "orders_by_delivery_type": dict(Counter(o["delivery_type"] for o in live)),
        "cancellation_rate": round(by_status.get("cancelled", 0) / total, 3) if total else 0,
        "average_order_value": round(sum(o["total_amount"] for o in live) / len(live), 2) if live else 0,
        "revenue_collected": round(sum(o["total_amount"] for o in paid), 2),
        "payment_pending": round(sum(o["total_amount"] for o in unpaid), 2),
        "revenue_by_payment_method": {m: round(sum(o["total_amount"] for o in paid if o["payment_method"] == m), 2)
                                      for m in {o["payment_method"] for o in paid}},
        "revenue_by_month": {k: round(v, 2) for k, v in sorted(by_month.items())},
    }


@app.get("/reports/top-products")
def top_products(limit: int = 5):
    agg = defaultdict(lambda: {"name": "", "unit": None, "quantity_sold": 0.0, "revenue": 0.0})
    for o in _live(_orders()):
        for i in o["items"]:
            a = agg[i["product_id"]]
            a["name"], a["unit"] = i["product_name"], i["unit"]
            a["quantity_sold"] = round(a["quantity_sold"] + i["quantity"], 3)
            a["revenue"] = round(a["revenue"] + i["line_total"], 2)
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:limit]
    return [{"product_id": pid, **data} for pid, data in ranked]


@app.get("/reports/top-customers")
def top_customers(limit: int = 5):
    spend = defaultdict(lambda: {"orders": 0, "total_spent": 0.0})
    for o in _live(_orders()):
        s = spend[o["user_id"]]
        s["orders"] += 1
        s["total_spent"] = round(s["total_spent"] + o["total_amount"], 2)
    ranked = sorted(spend.items(), key=lambda kv: kv[1]["total_spent"], reverse=True)[:limit]
    return [{"user_id": uid, **data} for uid, data in ranked]


@app.get("/reports/inventory")
def inventory_report(expiry_days: int = 7):
    names = _names()
    low = fetch(INVENTORY_URL, "/inventory/low-stock")
    expiring = fetch(INVENTORY_URL, "/inventory/expiring", {"days": expiry_days})
    return {
        "out_of_stock": [{"product_id": s["product_id"], "name": names.get(s["product_id"])}
                         for s in low if s["available"] <= 0],
        "low_stock": [{"product_id": s["product_id"], "name": names.get(s["product_id"]),
                       "available": s["available"], "reorder_level": s["reorder_level"]}
                      for s in low if s["available"] > 0],
        "expiring_soon": [{**b, "name": names.get(b["product_id"])} for b in expiring if not b["expired"]],
        "expired_not_written_off": [{**b, "name": names.get(b["product_id"])} for b in expiring if b["expired"]],
    }


@app.get("/reports/customer-requests")
def requests_report():
    reqs = fetch(CUSTOMER_URL, "/requests", {"limit": 1000})
    return {
        "total": len(reqs),
        "by_status": dict(Counter(r["status"] for r in reqs)),
        "by_type": dict(Counter(r["request_type"] for r in reqs)),
        "needs_attention": [{"id": r["id"], "type": r["request_type"], "subject": r["subject"],
                             "status": r["status"], "created_at": r["created_at"]}
                            for r in reqs if r["status"] in ("open", "in_progress")],
    }


@app.get("/reports/dashboard")
def dashboard():
    return {"sales": sales_report(), "top_products": top_products(), "top_customers": top_customers(),
            "inventory": inventory_report(), "customer_requests": requests_report()}
