from datetime import date, timedelta
from sqlalchemy.orm import Session
from .models import Doctor, Slot

SEED_DOCTORS = [
    ("Dr. Ananya Menon", "Cardiologist", "12 years", 800),
    ("Dr. Rahul Sharma", "Dermatologist", "9 years", 600),
    ("Dr. Meera Nair", "General Physician", "15 years", 500),
    ("Dr. Karthik Iyer", "Orthopedist", "11 years", 700),
]

def seed_database(db: Session):
    if db.query(Doctor).count():
        return
    for name, specialty, experience, fee in SEED_DOCTORS:
        doctor = Doctor(name=name, specialty=specialty, experience=experience, fee=fee)
        db.add(doctor)
        db.flush()
        for day in range(0, 5):
            appointment_date = (date.today() + timedelta(days=day)).isoformat()
            for time in ("09:30 AM", "11:00 AM", "02:00 PM", "04:30 PM"):
                db.add(Slot(doctor_id=doctor.id, date=appointment_date, time=time))
    db.commit()
