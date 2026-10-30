import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from supabase import Client, create_client

load_dotenv()
logger = logging.getLogger("clinic")

# ---------------------------------------------------------------
# الإعدادات (من Environment Variables)
# ---------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ADMIN_KEY = os.getenv("ADMIN_API_KEY")
# غيّرها لمنطقة عيادتك الزمنية، لأن Render شغال بتوقيت UTC
CLINIC_TZ = ZoneInfo(os.getenv("CLINIC_TIMEZONE", "Asia/Amman"))
# دومين الواجهة (مفصولة بفاصلة). "*" مقبولة بس أثناء التجربة
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase URL or Key is missing in environment variables!")
if not ADMIN_KEY:
    raise RuntimeError("ADMIN_API_KEY is missing in environment variables!")

# ---- تسجيل الدخول ----
JWT_SECRET = os.getenv("JWT_SECRET")
TOKEN_HOURS = 12            # مدة صلاحية الدخول (يوم عمل)
MAX_FAILS = 5               # محاولات فاشلة مسموحة
LOCK_SECONDS = 15 * 60      # مدة المنع بعد تجاوزها


def load_users() -> dict[str, bytes]:
    """ADMIN_USERS بصيغة:  secretary:HASH1,owner:HASH2"""
    users: dict[str, bytes] = {}
    for item in os.getenv("ADMIN_USERS", "").split(","):
        item = item.strip()
        if ":" in item:
            name, pw_hash = item.split(":", 1)
            users[name.strip()] = pw_hash.strip().encode()
    return users


USERS = load_users()
if not JWT_SECRET or not USERS:
    raise RuntimeError("JWT_SECRET or ADMIN_USERS is missing in environment variables!")

DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt())
FAILED_LOGINS: dict[str, list[float]] = {}   # IP -> أوقات المحاولات الفاشلة

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

OPEN_HOURS = list(range(10, 22))   # آخر موعد 21:00 - 22:00
CHAIRS = [1, 2, 3, 4]
BOOKING_WINDOW_DAYS = 7            # المريض بيحجز خلال 7 أيام (اليوم + 6)
VALID_STATUSES = ("confirmed", "arrived", "cancelled", "no_show")
PHONE_RE = re.compile(r"^[0-9+\-\s]{7,20}$")

app = FastAPI(title="Dental Clinic API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------
# نماذج البيانات
# ---------------------------------------------------------------
class AppointmentCreate(BaseModel):
    patient_name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=7, max_length=20)
    treatment_type: str = Field(default="غير محدد", max_length=80)
    appointment_date: date          # صيغة "YYYY-MM-DD"
    hour_slot: int                  # من 10 إلى 21


class AdminAppointmentCreate(AppointmentCreate):
    chair_number: Optional[int] = Field(default=None, ge=1, le=4)  # اختياري، لو فاضي بيختار تلقائياً


class StatusUpdate(BaseModel):
    status: Literal["confirmed", "arrived", "cancelled", "no_show"]


# ---------------------------------------------------------------
# دوال مساعدة
# ---------------------------------------------------------------
class LoginBody(BaseModel):
    username: str = Field(max_length=50)
    password: str = Field(max_length=100)


def require_admin(authorization: str = Header(None), x_admin_key: str = Header(None)) -> str:
    """بيقبل: توكن الدخول (Authorization: Bearer ...) أو مفتاح الطوارئ X-Admin-Key (للمالك فقط)"""
    if x_admin_key and secrets.compare_digest(x_admin_key, ADMIN_KEY):
        return "owner-key"
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=["HS256"])
            return str(payload["sub"])
        except jwt.PyJWTError:
            pass
    raise HTTPException(status_code=401, detail="غير مصرح")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")   # Render بيحط IP الزائر هون
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def now_clinic() -> datetime:
    return datetime.now(CLINIC_TZ)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="صيغة التاريخ لازم تكون YYYY-MM-DD")


def check_patient_window(d: date):
    today = now_clinic().date()
    last_day = today + timedelta(days=BOOKING_WINDOW_DAYS - 1)
    if d < today or d > last_day:
        raise HTTPException(status_code=400, detail="التاريخ خارج فترة الحجز (الأيام السبعة القادمة)")


def fetch_rows(columns: str, start: date, end: date) -> list[dict]:
    """كل المواعيد غير الملغاة بين تاريخين (شاملة)"""
    try:
        response = (
            supabase.table("appointments")
            .select(columns)
            .gte("appointment_date", start.isoformat())
            .lte("appointment_date", end.isoformat())
            .neq("status", "cancelled")
            .execute()
        )
    except Exception:
        logger.exception("Supabase select failed")
        raise HTTPException(status_code=500, detail="خطأ في الاتصال بقاعدة البيانات")
    return response.data or []


def book(data: AppointmentCreate, chairs: list[int]) -> dict:
    """بيجرّب الكراسي بالترتيب. الـ unique index بالقاعدة هو اللي بيمنع الحجز المزدوج"""
    for chair in chairs:
        payload = {
            "patient_name": data.patient_name.strip(),
            "phone": data.phone.strip(),
            "treatment_type": data.treatment_type.strip(),
            "appointment_date": data.appointment_date.isoformat(),
            "hour_slot": data.hour_slot,
            "chair_number": chair,
            "status": "confirmed",
        }
        try:
            response = supabase.table("appointments").insert(payload).execute()
            return {
                "message": "تم حجز الموعد بنجاح",
                "assigned_chair": chair,
                "details": response.data,
            }
        except APIError as e:
            if getattr(e, "code", None) == "23505":   # الكرسي انحجز للتو، جرّب اللي بعده
                continue
            logger.exception("Supabase insert failed")
            raise HTTPException(status_code=500, detail="حدث خطأ أثناء الحجز")
        except Exception:
            logger.exception("Supabase insert failed")
            raise HTTPException(status_code=500, detail="حدث خطأ أثناء الحجز")

    raise HTTPException(status_code=409, detail="عذراً، هذه الساعة ممتلئة بالكامل")


def validate_booking_basics(data: AppointmentCreate):
    if data.hour_slot not in OPEN_HOURS:
        raise HTTPException(
            status_code=400,
            detail=f"الساعة خارج أوقات العمل (من {OPEN_HOURS[0]}:00 حتى {OPEN_HOURS[-1] + 1}:00)",
        )
    if not PHONE_RE.fullmatch(data.phone.strip()):
        raise HTTPException(status_code=400, detail="رقم الهاتف غير صحيح")
    if not data.patient_name.strip():
        raise HTTPException(status_code=400, detail="الاسم مطلوب")


# ---------------------------------------------------------------
# مسارات عامة (المريض)
# ---------------------------------------------------------------
@app.get("/")
def read_root():
    return {"message": "Dental Clinic API is running successfully!"}


@app.get("/available-days")
def get_available_days():
    """الأيام السبعة القادمة، ولكل يوم الساعات اللي فيها كرسي شاغر (للمريض)"""
    now = now_clinic()
    today = now.date()
    days = [today + timedelta(days=i) for i in range(BOOKING_WINDOW_DAYS)]

    rows = fetch_rows("appointment_date, hour_slot", days[0], days[-1])
    taken: dict[tuple[str, int], int] = {}
    for r in rows:
        key = (r["appointment_date"], r["hour_slot"])
        taken[key] = taken.get(key, 0) + 1

    result = []
    for d in days:
        free_hours = []
        for h in OPEN_HOURS:
            if d == today and h <= now.hour:   # ساعة مضت أو شغّالة هلأ
                continue
            if taken.get((d.isoformat(), h), 0) < len(CHAIRS):
                free_hours.append(h)
        result.append({"date": d.isoformat(), "free_hours": free_hours, "free_count": len(free_hours)})

    return {"days": result}


@app.get("/available-slots/{appointment_date}")
def get_available_slots(appointment_date: str):
    """تفاصيل يوم واحد: كل ساعة وكم كرسي شاغر (بدون أسماء المرضى)"""
    d = parse_date(appointment_date)
    check_patient_window(d)

    rows = fetch_rows("hour_slot, chair_number", d, d)

    slots_status = []
    for hour in OPEN_HOURS:
        used = [r["chair_number"] for r in rows if r["hour_slot"] == hour]
        available_chairs = [c for c in CHAIRS if c not in used]
        slots_status.append({
            "hour_slot": hour,
            "max_chairs": len(CHAIRS),
            "booked_count": len(used),
            "available_chairs": available_chairs,
            "is_fully_booked": len(available_chairs) == 0,
        })

    return {"date": d.isoformat(), "schedule": slots_status}


@app.post("/appointments")
def create_appointment(appointment: AppointmentCreate):
    """حجز من المريض: أول كرسي شاغر تلقائياً، وبس خلال الأيام السبعة القادمة"""
    validate_booking_basics(appointment)
    check_patient_window(appointment.appointment_date)

    now = now_clinic()
    if appointment.appointment_date == now.date() and appointment.hour_slot <= now.hour:
        raise HTTPException(status_code=400, detail="هذه الساعة مضت أو بدأت")

    return book(appointment, CHAIRS)


# ---------------------------------------------------------------
# تسجيل الدخول
# ---------------------------------------------------------------
@app.post("/auth/login")
def login(body: LoginBody, request: Request):
    ip = client_ip(request)
    now = time.time()

    recent = [t for t in FAILED_LOGINS.get(ip, []) if now - t < LOCK_SECONDS]
    FAILED_LOGINS[ip] = recent
    if len(recent) >= MAX_FAILS:
        raise HTTPException(status_code=429, detail="محاولات كثيرة، جرّب بعد 15 دقيقة")

    stored = USERS.get(body.username)
    try:
        # بنفحص دايماً (حتى لو المستخدم مش موجود) حتى ما يعرف المهاجم أي أسماء صحيحة
        password_ok = bcrypt.checkpw(body.password.encode(), stored or DUMMY_HASH)
    except ValueError:
        logger.error("Invalid password hash format for user %s", body.username)
        password_ok = False

    if not stored or not password_ok:
        recent.append(now)
        raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة السر غير صحيحة")

    FAILED_LOGINS.pop(ip, None)
    expires_in = TOKEN_HOURS * 3600
    token = jwt.encode(
        {"sub": body.username, "exp": int(now) + expires_in},
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}


@app.get("/auth/me")
def me(user: str = Depends(require_admin)):
    """الواجهة بتستخدمه لتتأكد إنه الدخول لسا صالح"""
    return {"user": user}


# ---------------------------------------------------------------
# مسارات الإدارة (السكرتيرة) - تحتاج تسجيل دخول (Bearer token)
# ---------------------------------------------------------------
@app.get("/appointments", dependencies=[Depends(require_admin)])
def get_all_appointments(date: str):
    """كل مواعيد يوم معيّن مع الأسماء والحالة (ما عدا الملغاة)"""
    d = parse_date(date)
    try:
        response = (
            supabase.table("appointments")
            .select("*")
            .eq("appointment_date", d.isoformat())
            .order("hour_slot")
            .order("chair_number")
            .execute()
        )
    except Exception:
        logger.exception("Supabase select failed")
        raise HTTPException(status_code=500, detail="خطأ في الاتصال بقاعدة البيانات")
    return {"appointments": response.data or []}


@app.get("/admin/week", dependencies=[Depends(require_admin)])
def get_week(start: Optional[str] = None):
    """عرض أسبوعي: لكل يوم عدد المواعيد والسعة الكاملة. الافتراضي يبدأ من اليوم"""
    start_date = parse_date(start) if start else now_clinic().date()
    days = [start_date + timedelta(days=i) for i in range(7)]

    rows = fetch_rows("appointment_date", days[0], days[-1])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["appointment_date"]] = counts.get(r["appointment_date"], 0) + 1

    capacity = len(OPEN_HOURS) * len(CHAIRS)
    return {
        "days": [
            {"date": d.isoformat(), "booked": counts.get(d.isoformat(), 0), "capacity": capacity}
            for d in days
        ]
    }


@app.post("/admin/appointments", dependencies=[Depends(require_admin)])
def admin_create_appointment(appointment: AdminAppointmentCreate):
    """حجز من السكرتيرة: أي تاريخ، وممكن تحدد الكرسي"""
    validate_booking_basics(appointment)
    chairs = [appointment.chair_number] if appointment.chair_number else CHAIRS
    return book(appointment, chairs)


@app.patch("/admin/appointments/{appointment_id}", dependencies=[Depends(require_admin)])
def update_status(appointment_id: int, body: StatusUpdate):
    """تغيير الحالة: confirmed / arrived / cancelled / no_show"""
    try:
        response = (
            supabase.table("appointments")
            .update({"status": body.status})
            .eq("id", appointment_id)
            .execute()
        )
    except APIError as e:
        if getattr(e, "code", None) == "23505":
            raise HTTPException(status_code=409, detail="الكرسي صار محجوز بموعد ثاني، ما بنقدر نرجّع هالموعد")
        logger.exception("Supabase update failed")
        raise HTTPException(status_code=500, detail="خطأ أثناء التحديث")
    except Exception:
        logger.exception("Supabase update failed")
        raise HTTPException(status_code=500, detail="خطأ أثناء التحديث")

    if not response.data:
        raise HTTPException(status_code=404, detail="الموعد غير موجود")
    return {"message": "تم تحديث الحالة", "appointment": response.data[0]}