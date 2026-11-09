"""Customer Service  (port 8005)
Customer profile, saved addresses, and customer requests that store staff handle
(special product requests, complaints, questions, bulk orders)."""
import os
from datetime import datetime
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./customers.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

REQUEST_TYPES = ("product_request", "complaint", "query", "bulk_order", "feedback")
REQUEST_STATUSES = ("open", "in_progress", "resolved", "closed")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    auth_user_id = Column(Integer, unique=True, index=True, nullable=False)
    full_name = Column(String(150), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(30))
    created_at = Column(DateTime, default=datetime.utcnow)
    addresses = relationship("Address", cascade="all, delete-orphan", back_populates="customer")


class Address(Base):
    __tablename__ = "addresses"
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    label = Column(String(50), default="Home")
    line1 = Column(String(255), nullable=False)
    line2 = Column(String(255))
    city = Column(String(100), nullable=False)
    state = Column(String(100))
    postal_code = Column(String(20), nullable=False)
    is_default = Column(Boolean, default=False)
    customer = relationship("Customer", back_populates="addresses")


class CustomerRequest(Base):
    __tablename__ = "customer_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)       # auth user id of the customer
    request_type = Column(String(20), nullable=False, index=True)
    subject = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    product_id = Column(Integer)                                # optional related product
    order_id = Column(Integer)                                  # optional related order
    status = Column(String(20), default="open", index=True)
    staff_reply = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


Base.metadata.create_all(bind=engine)


class CustomerCreate(BaseModel):
    auth_user_id: int
    full_name: str
    email: EmailStr
    phone: Optional[str] = None


class CustomerUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None


class CustomerOut(CustomerCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


class AddressBase(BaseModel):
    label: str = "Home"
    line1: str
    line2: Optional[str] = None
    city: str
    state: Optional[str] = None
    postal_code: str
    is_default: bool = False


class AddressOut(AddressBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    customer_id: int


class RequestCreate(BaseModel):
    user_id: int
    request_type: Literal["product_request", "complaint", "query", "bulk_order", "feedback"]
    subject: str
    message: str
    product_id: Optional[int] = None
    order_id: Optional[int] = None


class RequestUpdate(BaseModel):                  # staff only (enforced by the gateway)
    status: Optional[Literal["open", "in_progress", "resolved", "closed"]] = None
    staff_reply: Optional[str] = None


class RequestOut(RequestCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    staff_reply: Optional[str]
    created_at: datetime
    updated_at: datetime


app = FastAPI(title="Green Basket Organic Store - Customer Service")


def _customer(db: Session, cid: int) -> Customer:
    c = db.get(Customer, cid)
    if not c:
        raise HTTPException(404, "Customer not found")
    return c


@app.get("/health")
def health():
    return {"service": "customer", "status": "ok"}


# ---------- customer requests (declared before /customers/{id} routes; different prefix) ----------
@app.post("/requests", response_model=RequestOut, status_code=201)
def create_request(body: RequestCreate, db: Session = Depends(get_db)):
    r = CustomerRequest(**body.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@app.get("/requests", response_model=list[RequestOut])
def list_requests(user_id: Optional[int] = None, status: Optional[str] = None,
                  request_type: Optional[str] = None, skip: int = 0,
                  limit: int = Query(100, le=1000), db: Session = Depends(get_db)):
    q = db.query(CustomerRequest)
    if user_id:
        q = q.filter(CustomerRequest.user_id == user_id)
    if status:
        q = q.filter(CustomerRequest.status == status)
    if request_type:
        q = q.filter(CustomerRequest.request_type == request_type)
    return q.order_by(CustomerRequest.id.desc()).offset(skip).limit(limit).all()


@app.get("/requests/{request_id}", response_model=RequestOut)
def get_request(request_id: int, db: Session = Depends(get_db)):
    r = db.get(CustomerRequest, request_id)
    if not r:
        raise HTTPException(404, "Request not found")
    return r


@app.patch("/requests/{request_id}", response_model=RequestOut)
def update_request(request_id: int, body: RequestUpdate, db: Session = Depends(get_db)):
    r = db.get(CustomerRequest, request_id)
    if not r:
        raise HTTPException(404, "Request not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    db.commit()
    db.refresh(r)
    return r


# ---------- profile ----------
@app.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(body: CustomerCreate, db: Session = Depends(get_db)):
    if db.query(Customer).filter_by(auth_user_id=body.auth_user_id).first():
        raise HTTPException(409, "Profile already exists for this account")
    c = Customer(**body.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@app.get("/customers", response_model=list[CustomerOut])
def list_customers(search: Optional[str] = None, skip: int = 0,
                   limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    q = db.query(Customer)
    if search:
        q = q.filter(Customer.full_name.like(f"%{search}%") | Customer.email.like(f"%{search}%"))
    return q.order_by(Customer.id).offset(skip).limit(limit).all()


@app.get("/customers/by-auth/{auth_user_id}", response_model=CustomerOut)
def by_auth(auth_user_id: int, db: Session = Depends(get_db)):
    c = db.query(Customer).filter_by(auth_user_id=auth_user_id).first()
    if not c:
        raise HTTPException(404, "Customer not found")
    return c


@app.get("/customers/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: int, db: Session = Depends(get_db)):
    return _customer(db, customer_id)


@app.put("/customers/{customer_id}", response_model=CustomerOut)
def update_customer(customer_id: int, body: CustomerUpdate, db: Session = Depends(get_db)):
    c = _customer(db, customer_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return c


@app.delete("/customers/{customer_id}", status_code=204)
def delete_customer(customer_id: int, db: Session = Depends(get_db)):
    db.delete(_customer(db, customer_id))
    db.commit()


# ---------- addresses ----------
def _only_default(db: Session, customer_id: int, keep_id: int):
    db.query(Address).filter(Address.customer_id == customer_id, Address.id != keep_id) \
      .update({Address.is_default: False})


@app.post("/customers/{customer_id}/addresses", response_model=AddressOut, status_code=201)
def add_address(customer_id: int, body: AddressBase, db: Session = Depends(get_db)):
    c = _customer(db, customer_id)
    a = Address(customer_id=customer_id, **body.model_dump())
    if not c.addresses:
        a.is_default = True                      # first address is the default
    db.add(a)
    db.flush()
    if a.is_default:
        _only_default(db, customer_id, a.id)
    db.commit()
    db.refresh(a)
    return a


@app.get("/customers/{customer_id}/addresses", response_model=list[AddressOut])
def list_addresses(customer_id: int, db: Session = Depends(get_db)):
    _customer(db, customer_id)
    return db.query(Address).filter_by(customer_id=customer_id).order_by(Address.id).all()


@app.put("/customers/{customer_id}/addresses/{address_id}", response_model=AddressOut)
def update_address(customer_id: int, address_id: int, body: AddressBase, db: Session = Depends(get_db)):
    a = db.get(Address, address_id)
    if not a or a.customer_id != customer_id:
        raise HTTPException(404, "Address not found")
    for k, v in body.model_dump().items():
        setattr(a, k, v)
    if a.is_default:
        _only_default(db, customer_id, a.id)
    db.commit()
    db.refresh(a)
    return a


@app.delete("/customers/{customer_id}/addresses/{address_id}", status_code=204)
def delete_address(customer_id: int, address_id: int, db: Session = Depends(get_db)):
    a = db.get(Address, address_id)
    if not a or a.customer_id != customer_id:
        raise HTTPException(404, "Address not found")
    db.delete(a)
    db.commit()
