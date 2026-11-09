"""Inventory Service  (port 8003)
Stock for perishable organic goods, tracked in batches with expiry dates.

* Stock is sold first-expiring-first-out (FEFO); expired batches are never sold.
* `available` = sellable quantity (non-expired). `quantity` = physically on the shelf.
* Every change is written to a movement log."""
import os
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, create_engine, func, or_
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./inventory.db")
DEFAULT_REORDER_LEVEL = float(os.getenv("DEFAULT_REORDER_LEVEL", "10"))
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
EPS = 1e-9


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class StockItem(Base):
    __tablename__ = "stock_items"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, unique=True, index=True, nullable=False)
    quantity = Column(Float, default=0, nullable=False)
    reorder_level = Column(Float, default=DEFAULT_REORDER_LEVEL, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StockBatch(Base):
    __tablename__ = "stock_batches"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, index=True, nullable=False)
    quantity_received = Column(Float, nullable=False)
    quantity_remaining = Column(Float, nullable=False)
    received_date = Column(Date, default=date.today)
    expiry_date = Column(Date, nullable=True)
    supplier = Column(String(150))
    unit_cost = Column(Float)


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, index=True, nullable=False)
    change = Column(Float, nullable=False)
    reason = Column(String(20), nullable=False)   # received|sold|returned|adjustment|wastage
    reference = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


class ReceiveIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)
    expiry_date: Optional[date] = None
    supplier: Optional[str] = None
    unit_cost: Optional[float] = Field(default=None, ge=0)


class QtyIn(BaseModel):
    quantity: float = Field(gt=0)
    reference: Optional[str] = None


class AdjustIn(BaseModel):
    delta: float
    reason: str = Field(min_length=3, description="e.g. stock count correction")


class SettingsIn(BaseModel):
    reorder_level: float = Field(ge=0)


class StockOut(BaseModel):
    product_id: int
    quantity: float
    available: float
    reorder_level: float
    is_low: bool


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    quantity_received: float
    quantity_remaining: float
    received_date: Optional[date]
    expiry_date: Optional[date]
    supplier: Optional[str]
    unit_cost: Optional[float]


class MovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    change: float
    reason: str
    reference: Optional[str]
    created_at: datetime


app = FastAPI(title="Green Basket Organic Store - Inventory Service")


# ---------- helpers ----------
def _not_expired():
    return or_(StockBatch.expiry_date.is_(None), StockBatch.expiry_date >= date.today())


def _available_map(db: Session, ids: Optional[list[int]] = None) -> dict[int, float]:
    q = db.query(StockBatch.product_id, func.coalesce(func.sum(StockBatch.quantity_remaining), 0.0)) \
          .filter(StockBatch.quantity_remaining > 0, _not_expired())
    if ids is not None:
        q = q.filter(StockBatch.product_id.in_(ids))
    return {pid: round(float(total), 3) for pid, total in q.group_by(StockBatch.product_id).all()}


def _out(s: StockItem, available: float) -> StockOut:
    return StockOut(product_id=s.product_id, quantity=round(s.quantity, 3), available=available,
                    reorder_level=s.reorder_level, is_low=available <= s.reorder_level)


def _stock(db: Session, pid: int, create: bool = False, lock: bool = False) -> Optional[StockItem]:
    q = db.query(StockItem).filter_by(product_id=pid)
    if lock:
        q = q.with_for_update()
    s = q.first()
    if not s and create:
        s = StockItem(product_id=pid, quantity=0, reorder_level=DEFAULT_REORDER_LEVEL)
        db.add(s)
        db.flush()
    return s


def _take(db: Session, pid: int, qty: float, include_expired: bool) -> float:
    """Remove qty from batches, soonest expiry first. Returns quantity that could not be taken."""
    q = db.query(StockBatch).filter(StockBatch.product_id == pid, StockBatch.quantity_remaining > 0)
    if not include_expired:
        q = q.filter(_not_expired())
    left = qty
    for b in q.order_by(StockBatch.expiry_date.is_(None), StockBatch.expiry_date, StockBatch.id).all():
        if left <= EPS:
            break
        used = min(b.quantity_remaining, left)
        b.quantity_remaining = round(b.quantity_remaining - used, 3)
        left -= used
    return left


def _put_new_batch(db: Session, pid: int, qty: float, expiry: Optional[date] = None,
                   supplier: Optional[str] = None, cost: Optional[float] = None):
    db.add(StockBatch(product_id=pid, quantity_received=qty, quantity_remaining=qty,
                      received_date=date.today(), expiry_date=expiry, supplier=supplier, unit_cost=cost))


def _put_back(db: Session, pid: int, qty: float):
    """Returned goods go back into the freshest still-valid batch (or a new 'returned' batch)."""
    b = db.query(StockBatch).filter(StockBatch.product_id == pid, StockBatch.expiry_date >= date.today()) \
          .order_by(StockBatch.expiry_date.desc()).first()
    if b:
        b.quantity_remaining = round(b.quantity_remaining + qty, 3)
    else:
        _put_new_batch(db, pid, qty, None, "returned")


def _log(db: Session, pid: int, change: float, reason: str, reference: Optional[str] = None):
    db.add(StockMovement(product_id=pid, change=round(change, 3), reason=reason, reference=reference))


# ---------- static routes first (before /inventory/{product_id}) ----------
@app.get("/health")
def health():
    return {"service": "inventory", "status": "ok"}


@app.get("/inventory/availability")
def availability(product_ids: str, db: Session = Depends(get_db)):
    """Public-safe: sellable quantity per product id, e.g. ?product_ids=1,2,3"""
    try:
        ids = [int(x) for x in product_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "product_ids must be comma separated integers")
    avail = _available_map(db, ids)
    return {str(i): avail.get(i, 0.0) for i in ids}


@app.post("/inventory/receive", response_model=StockOut, status_code=201)
def receive_stock(body: ReceiveIn, db: Session = Depends(get_db)):
    if body.expiry_date and body.expiry_date < date.today():
        raise HTTPException(422, "expiry_date is already in the past")
    s = _stock(db, body.product_id, create=True)
    _put_new_batch(db, body.product_id, body.quantity, body.expiry_date, body.supplier, body.unit_cost)
    s.quantity = round(s.quantity + body.quantity, 3)
    _log(db, body.product_id, body.quantity, "received", body.supplier)
    db.commit()
    return _out(s, _available_map(db, [body.product_id]).get(body.product_id, 0.0))


@app.get("/inventory/low-stock", response_model=list[StockOut])
def low_stock(db: Session = Depends(get_db)):
    avail = _available_map(db)
    rows = [_out(s, avail.get(s.product_id, 0.0)) for s in db.query(StockItem).all()]
    return sorted([r for r in rows if r.is_low], key=lambda r: r.available)


@app.get("/inventory/expiring")
def expiring(days: int = Query(7, ge=0, le=365), db: Session = Depends(get_db)):
    """Batches (with stock left) that expire within `days`, including ones already expired."""
    today = date.today()
    rows = db.query(StockBatch).filter(StockBatch.quantity_remaining > 0, StockBatch.expiry_date.isnot(None),
                                       StockBatch.expiry_date <= today + timedelta(days=days)) \
             .order_by(StockBatch.expiry_date).all()
    return [{"batch_id": b.id, "product_id": b.product_id, "quantity_remaining": b.quantity_remaining,
             "expiry_date": b.expiry_date, "days_left": (b.expiry_date - today).days,
             "expired": b.expiry_date < today} for b in rows]


@app.post("/inventory/write-off-expired")
def write_off_expired(db: Session = Depends(get_db)):
    """Remove all expired stock from the shelf and log it as wastage."""
    batches = db.query(StockBatch).filter(StockBatch.quantity_remaining > 0, StockBatch.expiry_date.isnot(None),
                                          StockBatch.expiry_date < date.today()).all()
    per_product: dict[int, dict] = {}
    for b in batches:
        d = per_product.setdefault(b.product_id, {"product_id": b.product_id, "quantity": 0.0, "batches": 0})
        d["quantity"] = round(d["quantity"] + b.quantity_remaining, 3)
        d["batches"] += 1
        b.quantity_remaining = 0
    for pid, d in per_product.items():
        s = _stock(db, pid, create=True)
        s.quantity = max(0.0, round(s.quantity - d["quantity"], 3))
        _log(db, pid, -d["quantity"], "wastage", "expired")
    db.commit()
    return {"written_off": list(per_product.values()),
            "total_quantity": round(sum(d["quantity"] for d in per_product.values()), 3)}


@app.get("/inventory", response_model=list[StockOut])
def list_stock(skip: int = 0, limit: int = Query(100, le=500), db: Session = Depends(get_db)):
    avail = _available_map(db)
    items = db.query(StockItem).order_by(StockItem.product_id).offset(skip).limit(limit).all()
    return [_out(s, avail.get(s.product_id, 0.0)) for s in items]


# ---------- per product ----------
@app.get("/inventory/{product_id}", response_model=StockOut)
def get_stock(product_id: int, db: Session = Depends(get_db)):
    s = _stock(db, product_id)
    if not s:
        raise HTTPException(404, "No stock record for this product")
    return _out(s, _available_map(db, [product_id]).get(product_id, 0.0))


@app.get("/inventory/{product_id}/batches", response_model=list[BatchOut])
def get_batches(product_id: int, db: Session = Depends(get_db)):
    return db.query(StockBatch).filter_by(product_id=product_id).order_by(StockBatch.id).all()


@app.get("/inventory/{product_id}/movements", response_model=list[MovementOut])
def get_movements(product_id: int, limit: int = Query(100, le=500), db: Session = Depends(get_db)):
    return db.query(StockMovement).filter_by(product_id=product_id) \
             .order_by(StockMovement.id.desc()).limit(limit).all()


@app.put("/inventory/{product_id}/settings", response_model=StockOut)
def set_reorder_level(product_id: int, body: SettingsIn, db: Session = Depends(get_db)):
    s = _stock(db, product_id, create=True)
    s.reorder_level = body.reorder_level
    db.commit()
    return _out(s, _available_map(db, [product_id]).get(product_id, 0.0))


@app.post("/inventory/{product_id}/adjust", response_model=StockOut)
def adjust(product_id: int, body: AdjustIn, db: Session = Depends(get_db)):
    """Stock-count correction. Positive adds a batch, negative removes (soonest-expiring first)."""
    if abs(body.delta) < EPS:
        raise HTTPException(422, "delta cannot be zero")
    s = _stock(db, product_id, create=True, lock=True)
    if body.delta > 0:
        _put_new_batch(db, product_id, body.delta, None, "adjustment")
    else:
        if _take(db, product_id, -body.delta, include_expired=True) > EPS:
            db.rollback()
            raise HTTPException(409, "Cannot remove more than is in stock")
    s.quantity = round(s.quantity + body.delta, 3)
    _log(db, product_id, body.delta, "adjustment", body.reason)
    db.commit()
    return _out(s, _available_map(db, [product_id]).get(product_id, 0.0))


@app.post("/inventory/{product_id}/reserve", response_model=StockOut)
def reserve(product_id: int, body: QtyIn, db: Session = Depends(get_db)):
    """Take stock for an order (used by the Order service). 409 if not enough sellable stock."""
    s = _stock(db, product_id, lock=True)
    if not s:
        raise HTTPException(404, "No stock record for this product")
    if _available_map(db, [product_id]).get(product_id, 0.0) + EPS < body.quantity:
        raise HTTPException(409, "Insufficient stock")
    _take(db, product_id, body.quantity, include_expired=False)
    s.quantity = round(s.quantity - body.quantity, 3)
    _log(db, product_id, -body.quantity, "sold", body.reference)
    db.commit()
    return _out(s, _available_map(db, [product_id]).get(product_id, 0.0))


@app.post("/inventory/{product_id}/release", response_model=StockOut)
def release(product_id: int, body: QtyIn, db: Session = Depends(get_db)):
    """Give stock back (cancelled order)."""
    s = _stock(db, product_id, create=True, lock=True)
    _put_back(db, product_id, body.quantity)
    s.quantity = round(s.quantity + body.quantity, 3)
    _log(db, product_id, body.quantity, "returned", body.reference)
    db.commit()
    return _out(s, _available_map(db, [product_id]).get(product_id, 0.0))
