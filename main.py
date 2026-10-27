from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
# تأكد 100% أن هذا المتغير في Render يحتوي على الـ service_role وليس الـ anon
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase URL or Key is missing in environment variables!")

# تعديل طريقة إنشاء العميل لضمان إرسال الطلبات للمخطط العام بشكل صريح ومستقر
supabase: Client = create_client(
    SUPABASE_URL, 
    SUPABASE_KEY,
    options={"schema": "public"}
)

app = FastAPI(title="Dental Clinic API", version="1.0")

class AppointmentCreate(BaseModel):
    patient_name: str
    phone: str
    treatment_type: str
    appointment_date: str  # صيغة "YYYY-MM-DD"
    hour_slot: int         # من 10 إلى 22

@app.get("/")
def read_root():
    return {"message": "Dental Clinic API is running successfully!"}

# 1. جلب المواعيد المتاحة ليوم معين
@app.get("/available-slots/{appointment_date}")
def get_available_slots(appointment_date: str):
    working_hours = list(range(10, 23))
    try:
        response = supabase.table("appointments").select("hour_slot, chair_number").eq("appointment_date", appointment_date).eq("status", "confirmed").execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase Error: {str(e)}")
    
    booked_appointments = response.data or []
    
    slots_status = []
    for hour in working_hours:
        chairs_in_hour = []
        for b in booked_appointments:
            if isinstance(b, dict):
                h_slot = b.get("hour_slot")
                c_num = b.get("chair_number")
                if h_slot == hour and c_num is not None and str(c_num).isdigit():
                    chairs_in_hour.append(int(c_num))
                    
        available_chairs = [c for c in [1, 2, 3, 4] if c not in chairs_in_hour]
        
        slots_status.append({
            "hour_slot": hour,
            "max_chairs": 4,
            "booked_count": len(chairs_in_hour),
            "available_chairs": available_chairs,
            "is_fully_booked": len(available_chairs) == 0
        })
        
    return {"date": appointment_date, "schedule": slots_status}

# 2. حجز موعد جديد للمريض (اختيار أول كرسي شاغر تلقائياً)
@app.post("/appointments")
def create_appointment(appointment: AppointmentCreate):
    if not (10 <= appointment.hour_slot <= 22):
        raise HTTPException(status_code=400, detail="الساعة خارج أوقات العمل (الدوام من 10 صباحاً حتى 10 مساءً)")
    
    try:
        response = supabase.table("appointments").select("chair_number").eq("appointment_date", appointment.appointment_date).eq("hour_slot", appointment.hour_slot).eq("status", "confirmed").execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase Select Error: {str(e)}")
    
    booked_chairs = []
    for b in (response.data or []):
        if isinstance(b, dict) and b.get("chair_number") is not None:
            c_num = b.get("chair_number")
            if str(c_num).isdigit():
                booked_chairs.append(int(c_num))
                
    if len(booked_chairs) >= 4:
        raise HTTPException(status_code=400, detail="عذراً، هذه الساعة ممتلئة بالكامل (4 كراسي محجوزة)")
    
    assigned_chair = None
    for c in:
        if c not in booked_chairs:
            assigned_chair = c
            break
            
    try:
        # تحويل البيانات إلى قاموس بايثون صافي ونظيف لمنع أخطاء الـ JSON وعمود الهاتف المضاف حديثاً
        payload = {
            "patient_name": str(appointment.patient_name),
            "phone": str(appointment.phone) if appointment.phone else None,
            "treatment_type": str(appointment.treatment_type),
            "appointment_date": str(appointment.appointment_date),
            "hour_slot": int(appointment.hour_slot),
            "chair_number": int(assigned_chair) if assigned_chair else None,
            "status": "confirmed"
        }
        
        insert_response = supabase.table("appointments").insert(payload).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase Insert Error: {str(e)}")
    
    return {
        "message": "تم حجز الموعد بنجاح",
        "assigned_chair": assigned_chair,
        "details": insert_response.data
    }

# 3. جلب جميع مواعيد اليوم للآدمن
@app.get("/appointments")
def get_all_appointments(date: str):
    try:
        response = supabase.table("appointments").select("*").eq("appointment_date", date).order("hour_slot").execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase Error: {str(e)}")
    return {"appointments": response.data or []}
