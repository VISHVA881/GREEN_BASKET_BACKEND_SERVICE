"""Order Service  (port 8004)
Customers place delivery or pickup orders. Prices come from Product Listings; stock is
reserved in Inventory (soonest-expiring batch first).

Status flow:  pending -> confirmed -> packed -> out_for_delivery -> delivered
              (pickup orders go packed -> delivered;  pending/confirmed/packed -> cancelled)
Cancelling gives the stock back. Cash-on-delivery orders are marked paid on delivery."""
import os
from datetime import datetime
from typing import Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./orders.db")
LISTINGS_URL = os.getenv("LISTINGS_URL", "http://localhost:8002")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8003")
DELIVERY_FEE = float(os.getenv("DELIVERY_FEE", "40"))
FREE_DELIVERY_ABOVE = float(os.getenv("FREE_DELIVERY_ABOVE", "500"))

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"packed", "cancelled"},
    "packed": {"out_for_delivery", "delivered", "cancelled"},
    "out_for_delivery": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}
PAYMENT_STATUSES = {"unpaid", "paid", "refunded"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    status = Column(String(20), default="pending", index=True)
    delivery_type = Column(String(10), default="delivery")        # delivery | pickup
    payment_method = Column(String(10), default="cod")            # cod | upi | card
    payment_status = Column(String(10), default="unpaid")
    subtotal = Column(Float, nullable=False)
    delivery_fee = Column(Float, nullable=False)
    total_amount = Column(Float, nullable=False)
    delivery_address = Column(Text)
    notes = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    items = relationship("OrderItem", cascade="all, delete-orphan", back_populates="order")
    events = relationship("OrderEvent", cascade="all, delete-orphan", order_by="OrderEvent.id")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    product_id = Column(Integer, nullable=False, index=True)
    product_name = Column(String(200), nullable=False)
    unit = Column(String(20))
    unit_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    line_total = Column(Float, nullable=False)
    order = relationship("Order", back_populates="items")


class OrderEvent(Base):
    __tablename__ = "order_events"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False)
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


class ItemIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0, le=50)       # decimals allowed (e.g. 0.5 kg)


class OrderCreate(BaseModel):
    user_id: int
    items: list[ItemIn] = Field(min_length=1)
    delivery_type: Literal["delivery", "pickup"] = "delivery"
    payment_method: Literal["cod", "upi", "card"] = "cod"
    delivery_address: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def need_address(self):
        if self.delivery_type == "delivery" and not (self.delivery_address and len(self.delivery_address.strip()) >= 5):
            raise ValueError("delivery_address is required for delivery orders")
        return self


class StatusIn(BaseModel):
    status: str
    note: Optional[str] = None


class PaymentStatusIn(BaseModel):
    payment_status: str


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    product_name: str
    unit: Optional[str]
    unit_price: float
    quantity: float
    line_total: float


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    note: Optional[str]
    created_at: datetime


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    status: str
    delivery_type: str
    payment_method: str
    payment_status: str
    subtotal: float
    delivery_fee: float
    total_amount: float
    delivery_address: Optional[str]
    notes: Optional[str]
    created_at: datetime
    items: list[ItemOut]


app = FastAPI(title="Green Basket Organic Store - Order Service")


def _call(method: str, url: str, **kw) -> httpx.Response:
    try:
        return httpx.request(method, url, timeout=8, **kw)
    except httpx.HTTPError:
        raise HTTPException(502, "A required service is unavailable")


def _give_back(lines: list[tuple[int, float]], reference: str):
    for pid, qty in lines:
        try:
            httpx.post(f"{INVENTORY_URL}/inventory/{pid}/release",
                       json={"quantity": qty, "reference": reference}, timeout=8)
        except httpx.HTTPError:
            pass  # best effort


def _order(db: Session, order_id: int) -> Order:
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    return o


def _move(db: Session, o: Order, new_status: str, note: Optional[str] = None):
    if new_status not in TRANSITIONS:
        raise HTTPException(422, f"status must be one of {sorted(TRANSITIONS)}")
    if new_status not in TRANSITIONS[o.status]:
        raise HTTPException(400, f"Cannot change order from '{o.status}' to '{new_status}'")
    if new_status == "out_for_delivery" and o.delivery_type == "pickup":
        raise HTTPException(400, "Pickup orders go straight from 'packed' to 'delivered'")
    o.status = new_status
    db.add(OrderEvent(order_id=o.id, status=new_status, note=note))
    if new_status == "cancelled":
        _give_back([(i.product_id, i.quantity) for i in o.items], f"order:{o.id}:cancelled")
        if o.payment_status == "paid":
            o.payment_status = "refunded"
    if new_status == "delivered" and o.payment_method == "cod":
        o.payment_status = "paid"               # cash collected at the door / counter
    db.commit()
    db.refresh(o)


@app.get("/health")
def health():
    return {"service": "order", "status": "ok"}


@app.post("/orders", response_model=OrderOut, status_code=201)
def place_order(body: OrderCreate, db: Session = Depends(get_db)):
    wanted: dict[int, float] = {}
    for it in body.items:                         # merge duplicate lines
        wanted[it.product_id] = round(wanted.get(it.product_id, 0) + it.quantity, 3)

    # 1) look up every product and price the basket
    lines: list[OrderItem] = []
    for pid, qty in wanted.items():
        r = _call("GET", f"{LISTINGS_URL}/products/{pid}")
        if r.status_code == 404:
            raise HTTPException(404, f"Product {pid} not found")
        if r.status_code != 200:
            raise HTTPException(502, "Product listing service error")
        prod = r.json()
        if not prod["is_active"]:
            raise HTTPException(400, f"'{prod['name']}' is not available")
        lines.append(OrderItem(product_id=pid, product_name=prod["name"], unit=prod["unit"],
                               unit_price=prod["price"], quantity=qty, line_total=round(prod["price"] * qty, 2)))

    subtotal = round(sum(l.line_total for l in lines), 2)
    fee = 0.0 if body.delivery_type == "pickup" or subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_FEE
    order = Order(user_id=body.user_id, delivery_type=body.delivery_type, payment_method=body.payment_method,
                  subtotal=subtotal, delivery_fee=fee, total_amount=round(subtotal + fee, 2),
                  delivery_address=body.delivery_address, notes=body.notes, items=lines)
    order.events.append(OrderEvent(status="pending", note="Order placed"))
    db.add(order)
    db.flush()                                    # gives us order.id for the stock reference

    # 2) reserve stock; undo everything if any line fails
    reserved: list[tuple[int, float]] = []
    try:
        for l in lines:
            rr = _call("POST", f"{INVENTORY_URL}/inventory/{l.product_id}/reserve",
                       json={"quantity": l.quantity, "reference": f"order:{order.id}"})
            if rr.status_code in (404, 409):
                raise HTTPException(409, f"Not enough stock for '{l.product_name}'")
            if rr.status_code != 200:
                raise HTTPException(502, "Could not reserve stock")
            reserved.append((l.product_id, l.quantity))
        db.commit()
    except BaseException:
        db.rollback()
        _give_back(reserved, "order:failed")
        raise
    db.refresh(order)
    return order


@app.get("/orders", response_model=list[OrderOut])
def list_orders(user_id: Optional[int] = None, status: Optional[str] = None,
                skip: int = 0, limit: int = Query(100, le=1000), db: Session = Depends(get_db)):
    q = db.query(Order)
    if user_id:
        q = q.filter(Order.user_id == user_id)
    if status:
        q = q.filter(Order.status == status)
    return q.order_by(Order.id.desc()).offset(skip).limit(limit).all()


@app.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, db: Session = Depends(get_db)):
    return _order(db, order_id)


@app.get("/orders/{order_id}/tracking", response_model=list[EventOut])
def tracking(order_id: int, db: Session = Depends(get_db)):
    return _order(db, order_id).events


@app.patch("/orders/{order_id}/status", response_model=OrderOut)
def set_status(order_id: int, body: StatusIn, db: Session = Depends(get_db)):
    o = _order(db, order_id)
    _move(db, o, body.status, body.note)
    return o


@app.patch("/orders/{order_id}/payment", response_model=OrderOut)
def set_payment_status(order_id: int, body: PaymentStatusIn, db: Session = Depends(get_db)):
    if body.payment_status not in PAYMENT_STATUSES:
        raise HTTPException(422, f"payment_status must be one of {sorted(PAYMENT_STATUSES)}")
    o = _order(db, order_id)
    o.payment_status = body.payment_status
    db.commit()
    db.refresh(o)
    return o


@app.post("/orders/{order_id}/cancel", response_model=OrderOut)
def cancel(order_id: int, db: Session = Depends(get_db)):
    o = _order(db, order_id)
    _move(db, o, "cancelled", "Cancelled")
    return o
