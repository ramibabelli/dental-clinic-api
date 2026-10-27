from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
from dotenv import load_dotenv
from datetime import date

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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

# 1. جلب المواعيد المتاحة ليوم معين (تحديد الكراسي المشغولة من أصل 4)
@app.get("/available-slots/{appointment_date}")
def get_available_slots(appointment_date: str):
    # أوقات العمل من 10 صباحاً (10) إلى 10 مساءً (22)
    working_hours = list(range(10, 23))
    
    # جلب الحجوزات الموجودة في هذا اليوم من Supabase
    response = supabase.table("appointments").select("hour_slot, chair_number").eq("appointment_date", appointment_date).eq("status", "confirmed").execute()
    
    booked_appointments = response.data
    
    slots_status = []
    for hour in working_hours:
        # كم كرسي محجوز في هذه الساعة؟
        chairs_in_hour = [int(b["chair_number"]) for b in booked_appointments if b.get("hour_slot") == hour and b.get("chair_number") is not None]
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
    # التأكد أن الساعة ضمن الدوام (10 إلى 22)
    if not (10 <= appointment.hour_slot <= 22):
        raise HTTPException(status_code=400, detail="الساعة خارج أوقات العمل (الدوام من 10 صباحاً حتى 10 مساءً)")
    
    # التحقق من الكراسي المحجوزة في هذه الساعة وذلك اليوم
    response = supabase.table("appointments").select("chair_number").eq("appointment_date", appointment.appointment_date).eq("hour_slot", appointment.hour_slot).eq("status", "confirmed").execute()
    
    booked_chairs = []
    for b in response.data:
        if isinstance(b, dict):
            val = b.get("chair_number")
            if val is not None and str(val).isdigit():
                booked_chairs.append(int(val))
        else:
            if b is not None and str(b).isdigit():
                booked_chairs.append(int(b))
                
    if len(booked_chairs) >= 4:
        raise HTTPException(status_code=400, detail="عذراً، هذه الساعة ممتلئة بالكامل (4 كراسي محجوزة)")
    
    # إيجاد أول كرسي متاح (من 1 إلى 4)
    assigned_chair = None
    for c in [1, 2, 3, 4]:
        if c not in booked_chairs:
            assigned_chair = c
            break
            
    # إدخال الموعد في Supabase
    insert_response = supabase.table("appointments").insert({
        "patient_name": appointment.patient_name,
        "phone": appointment.phone,
        "treatment_type": appointment.treatment_type,
        "appointment_date": appointment.appointment_date,
        "hour_slot": appointment.hour_slot,
        "chair_number": assigned_chair,
        "status": "confirmed"
    }).execute()
    
    return {
        "message": "تم حجز الموعد بنجاح",
        "assigned_chair": assigned_chair,
        "details": insert_response.data
    }

# 3. جلب جميع موعد اليوم للآدمن (السكرتيرة)
@app.get("/appointments")
def get_all_appointments(date: str):
    response = supabase.table("appointments").select("*").eq("appointment_date", date).order("hour_slot").execute()
    return {"appointments": response.data}