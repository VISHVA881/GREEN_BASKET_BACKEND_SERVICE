"""Authentication (Login) Service  (port 8001)
Register, login (JWT), current user, token verification.
Roles: customer (default) | staff (store staff / manager)."""
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./auth.db")
# MySQL example: mysql+pymysql://user:password@localhost:3306/green_basket_auth
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = int(os.getenv("TOKEN_EXPIRE_MINUTES", "60"))
# Registering with this code creates a staff account. Unset = staff sign-up disabled.
STAFF_SIGNUP_CODE = os.getenv("STAFF_SIGNUP_CODE")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class AuthUser(Base):
    __tablename__ = "auth_users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), default="customer", nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    staff_code: Optional[str] = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    role: str
    is_active: bool
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class VerifyIn(BaseModel):
    token: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=72)


def create_token(user: AuthUser) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user.id), "email": user.email, "role": user.role, "exp": exp}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


def current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> AuthUser:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    data = decode_token(token)
    user = db.get(AuthUser, int(data["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    return user


app = FastAPI(title="Green Basket Organic Store - Authentication Service")


@app.get("/health")
def health():
    return {"service": "authentication", "status": "ok"}


@app.post("/auth/register", response_model=UserOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    role = "customer"
    if body.staff_code is not None:
        if not STAFF_SIGNUP_CODE or not secrets.compare_digest(body.staff_code, STAFF_SIGNUP_CODE):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid staff code")
        role = "staff"
    if db.query(AuthUser).filter(AuthUser.email == body.email.lower()).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = AuthUser(email=body.email.lower(), hashed_password=pwd_context.hash(body.password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(AuthUser).filter(AuthUser.email == body.email.lower()).first()
    if not user or not pwd_context.verify(body.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")
    return TokenOut(access_token=create_token(user), expires_in=TOKEN_EXPIRE_MINUTES * 60)


@app.get("/auth/me", response_model=UserOut)
def me(user: AuthUser = Depends(current_user)):
    return user


@app.post("/auth/verify")
def verify(body: VerifyIn):
    data = decode_token(body.token)
    return {"valid": True, "user_id": int(data["sub"]), "role": data.get("role")}


@app.post("/auth/change-password", status_code=204)
def change_password(body: ChangePasswordIn, user: AuthUser = Depends(current_user),
                    db: Session = Depends(get_db)):
    if not pwd_context.verify(body.old_password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Old password is incorrect")
    user.hashed_password = pwd_context.hash(body.new_password)
    db.commit()
