from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, date

from .database import SessionLocal
from .models import User, Timesheet
from .auth import hash_password, verify_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Invalid session")
    return user


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    return get_current_user(request, db)


def require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


def recompute_timesheet(ts: Timesheet):
    if ts.clock_in and ts.clock_out:
        total_hours = (ts.clock_out - ts.clock_in).total_seconds() / 3600
        ts.total_hours = round(max(total_hours, 0), 2)
        ts.pto_earned = round(ts.total_hours * 0.05, 2)
    else:
        ts.total_hours = 0
        ts.pto_earned = 0


def parse_dt_local(dt_str: str) -> datetime:
    return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")


@router.get("/setup-admin")
def setup_admin(db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == "admin@a4l.local").first()
    if existing:
        return {"status": "admin already exists"}

    admin = User(
        name="Admin",
        email="admin@a4l.local",
        password_hash=hash_password("ChangeMe123!"),
        role="admin",
    )
    db.add(admin)
    db.commit()
    return {"status": "admin created", "login": "admin@a4l.local / ChangeMe123!"}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password"})

    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_login(request, db)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)

    if user.role == "admin":
        return RedirectResponse(url="/admin", status_code=303)
    return RedirectResponse(url="/me", status_code=303)


@router.get("/me", response_class=HTMLResponse)
def my_dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)

    rows = (
        db.query(Timesheet)
        .filter(Timesheet.user_id == user.id)
        .order_by(Timesheet.clock_in.desc().nullslast(), Timesheet.id.desc())
        .all()
    )

    total_hours = round(sum(r.total_hours or 0 for r in rows), 2)
    pto_balance = round(sum(r.pto_earned or 0 for r in rows), 2)

    open_shift = next((r for r in rows if r.clock_in and not r.clock_out), None)

    return templates.TemplateResponse(
        "user_dashboard.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "total_hours": total_hours,
            "pto_balance": pto_balance,
            "open_shift": open_shift,
        },
    )


@router.post("/me/clock-in")
def me_clock_in(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)

    open_shift = (
        db.query(Timesheet)
        .filter(Timesheet.user_id == user.id, Timesheet.clock_in.isnot(None), Timesheet.clock_out.is_(None))
        .first()
    )
    if open_shift:
        return RedirectResponse(url="/me", status_code=303)

    ts = Timesheet(user_id=user.id, date=date.today(), clock_in=datetime.now())
    db.add(ts)
    db.commit()
    return RedirectResponse(url="/me", status_code=303)


@router.post("/me/clock-out/{timesheet_id}")
def me_clock_out(timesheet_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)

    ts = db.query(Timesheet).filter(Timesheet.id == timesheet_id, Timesheet.user_id == user.id).first()
    if not ts or not ts.clock_in or ts.clock_out:
        return RedirectResponse(url="/me", status_code=303)

    ts.clock_out = datetime.now()
    recompute_timesheet(ts)
    db.commit()
    return RedirectResponse(url="/me", status_code=303)


@router.get("/timesheet/{timesheet_id}/edit", response_class=HTMLResponse)
def edit_timesheet_page(timesheet_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)

    ts = db.query(Timesheet).filter(Timesheet.id == timesheet_id).first()
    if not ts:
        raise HTTPException(404, "Not found")

    if user.role != "admin" and ts.user_id != user.id:
        raise HTTPException(403, "Not allowed")

    return templates.TemplateResponse("edit_timesheet.html", {"request": request, "user": user, "ts": ts})


@router.post("/timesheet/{timesheet_id}/edit")
def edit_timesheet_submit(
    timesheet_id: int,
    request: Request,
    clock_in: str = Form(""),
    clock_out: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)

    ts = db.query(Timesheet).filter(Timesheet.id == timesheet_id).first()
    if not ts:
        raise HTTPException(404, "Not found")

    if user.role != "admin" and ts.user_id != user.id:
        raise HTTPException(403, "Not allowed")

    ts.clock_in = parse_dt_local(clock_in) if clock_in else None
    ts.clock_out = parse_dt_local(clock_out) if clock_out else None
    recompute_timesheet(ts)
    db.commit()

    return RedirectResponse(url="/admin" if user.role == "admin" else "/me", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_admin(user)

    users = db.query(User).order_by(User.name.asc()).all()
    timesheets = (
        db.query(Timesheet)
        .order_by(Timesheet.clock_in.desc().nullslast(), Timesheet.id.desc())
        .limit(300)
        .all()
    )

    balances = {}
    for u in users:
        rows = db.query(Timesheet).filter(Timesheet.user_id == u.id).all()
        balances[u.id] = {
            "hours": round(sum(r.total_hours or 0 for r in rows), 2),
            "pto": round(sum(r.pto_earned or 0 for r in rows), 2),
        }

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "timesheets": timesheets,
            "balances": balances,
        },
    )


@router.post("/admin/create-user")
def admin_create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_login(request, db)
    require_admin(admin)

    email_clean = email.strip().lower()
    if db.query(User).filter(User.email == email_clean).first():
        return RedirectResponse(url="/admin", status_code=303)

    u = User(
        name=name.strip(),
        email=email_clean,
        password_hash=hash_password(password),
        role="user",
    )
    db.add(u)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)
