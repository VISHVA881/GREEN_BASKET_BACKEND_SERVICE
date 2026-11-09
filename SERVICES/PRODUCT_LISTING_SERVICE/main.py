"""Product Listings Service  (port 8002)
Organic products customers browse. Stock quantities live in the Inventory service;
`include_stock=true` asks Inventory for the sellable quantity of each listed product."""
import os
from typing import Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./listings.db")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8003")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

Unit = Literal["kg", "g", "piece", "bunch", "dozen", "litre", "pack"]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False, index=True)
    price = Column(Float, nullable=False)                 # price per unit
    unit = Column(String(20), default="kg", nullable=False)
    is_organic = Column(Boolean, default=True)
    certification = Column(String(100))                   # e.g. "India Organic", "USDA Organic"
    origin = Column(String(150))                          # farm / region
    shelf_life_days = Column(Integer)
    description = Column(Text)
    image_url = Column(String(500))
    is_active = Column(Boolean, default=True)


Base.metadata.create_all(bind=engine)


class CategoryBase(BaseModel):
    name: str
    description: Optional[str] = None


class CategoryOut(CategoryBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


class ProductBase(BaseModel):
    sku: str
    name: str
    category_id: int
    price: float = Field(gt=0)
    unit: Unit = "kg"
    is_organic: bool = True
    certification: Optional[str] = None
    origin: Optional[str] = None
    shelf_life_days: Optional[int] = Field(default=None, gt=0)
    description: Optional[str] = None
    image_url: Optional[str] = None
    is_active: bool = True


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    category_id: Optional[int] = None
    price: Optional[float] = Field(default=None, gt=0)
    unit: Optional[Unit] = None
    is_organic: Optional[bool] = None
    certification: Optional[str] = None
    origin: Optional[str] = None
    shelf_life_days: Optional[int] = Field(default=None, gt=0)
    description: Optional[str] = None
    image_url: Optional[str] = None
    is_active: Optional[bool] = None


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    available_quantity: Optional[float] = None    # filled only when include_stock=true


app = FastAPI(title="Green Basket Organic Store - Product Listing Service")


def _with_stock(products: list[Product]) -> list[ProductOut]:
    out = [ProductOut.model_validate(p) for p in products]
    if not out:
        return out
    try:
        r = httpx.get(f"{INVENTORY_URL}/inventory/availability",
                      params={"product_ids": ",".join(str(p.id) for p in out)}, timeout=3)
        if r.status_code == 200:
            avail = r.json()
            for p in out:
                p.available_quantity = avail.get(str(p.id), 0.0)
    except httpx.HTTPError:
        pass  # inventory down: still show the listing, just without quantities
    return out


@app.get("/health")
def health():
    return {"service": "product-listing", "status": "ok"}


# ---------- categories ----------
@app.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryBase, db: Session = Depends(get_db)):
    if db.query(Category).filter_by(name=body.name).first():
        raise HTTPException(409, "Category already exists")
    c = Category(**body.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@app.get("/categories", response_model=list[CategoryOut])
def list_categories(db: Session = Depends(get_db)):
    return db.query(Category).order_by(Category.name).all()


@app.put("/categories/{cat_id}", response_model=CategoryOut)
def update_category(cat_id: int, body: CategoryBase, db: Session = Depends(get_db)):
    c = db.get(Category, cat_id)
    if not c:
        raise HTTPException(404, "Category not found")
    c.name, c.description = body.name, body.description
    db.commit()
    db.refresh(c)
    return c


@app.delete("/categories/{cat_id}", status_code=204)
def delete_category(cat_id: int, db: Session = Depends(get_db)):
    c = db.get(Category, cat_id)
    if not c:
        raise HTTPException(404, "Category not found")
    if db.query(Product).filter_by(category_id=cat_id).first():
        raise HTTPException(409, "Category still has products")
    db.delete(c)
    db.commit()


# ---------- products ----------
@app.post("/products", response_model=ProductOut, status_code=201)
def create_product(body: ProductBase, db: Session = Depends(get_db)):
    if not db.get(Category, body.category_id):
        raise HTTPException(404, "Category not found")
    if db.query(Product).filter_by(sku=body.sku).first():
        raise HTTPException(409, "SKU already exists")
    p = Product(**body.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return ProductOut.model_validate(p)


@app.get("/products", response_model=list[ProductOut])
def list_products(category_id: Optional[int] = None, search: Optional[str] = None,
                  min_price: Optional[float] = None, max_price: Optional[float] = None,
                  certification: Optional[str] = None, include_stock: bool = False,
                  active_only: bool = True, skip: int = 0, limit: int = Query(50, le=200),
                  db: Session = Depends(get_db)):
    q = db.query(Product)
    if active_only:
        q = q.filter(Product.is_active.is_(True))
    if category_id:
        q = q.filter(Product.category_id == category_id)
    if search:
        q = q.filter(Product.name.like(f"%{search}%"))
    if min_price is not None:
        q = q.filter(Product.price >= min_price)
    if max_price is not None:
        q = q.filter(Product.price <= max_price)
    if certification:
        q = q.filter(Product.certification == certification)
    items = q.order_by(Product.id).offset(skip).limit(limit).all()
    return _with_stock(items) if include_stock else [ProductOut.model_validate(p) for p in items]


@app.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, include_stock: bool = False, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    return _with_stock([p])[0] if include_stock else ProductOut.model_validate(p)


@app.put("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, body: ProductUpdate, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    data = body.model_dump(exclude_unset=True)
    if "category_id" in data and not db.get(Category, data["category_id"]):
        raise HTTPException(404, "Category not found")
    if "sku" in data and db.query(Product).filter(Product.sku == data["sku"], Product.id != product_id).first():
        raise HTTPException(409, "SKU already exists")
    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return ProductOut.model_validate(p)


@app.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: int, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    db.delete(p)
    db.commit()
