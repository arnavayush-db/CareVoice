
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from .models import Doctor, Slot, Appointment


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def list_doctors(db: Session):
    return db.query(Doctor).order_by(Doctor.id).all()


def is_slot_in_future(slot: Slot) -> bool:
    """
    Check whether a slot is in the future.
    Expected date format: YYYY-MM-DD
    Expected time format: 09:30 AM
    """
    try:
        slot_datetime = datetime.strptime(
            f"{slot.date} {slot.time}",
            "%Y-%m-%d %I:%M %p"
        )

        return slot_datetime > datetime.now()

    except ValueError:
        # Invalid date/time should not be offered.
        return False


def available_slots(
    db: Session,
    doctor_id: int | None = None,
    date: str | None = None
):
    query = (
        db.query(Slot, Doctor)
        .join(Doctor)
        .filter(Slot.is_booked == 0)
    )

    if doctor_id:
        query = query.filter(Slot.doctor_id == doctor_id)

    if date:
        query = query.filter(Slot.date == date)

    results = query.order_by(
        Slot.date,
        Slot.time
    ).all()

    future_slots = []

    for slot, doctor in results:

        if not is_slot_in_future(slot):
            continue

        future_slots.append({
            "id": slot.id,
            "doctor_id": doctor.id,
            "doctor": doctor.name,
            "specialty": doctor.specialty,
            "date": slot.date,
            "time": slot.time
        })

    return future_slots


def book(
    db: Session,
    slot_id: int,
    patient_name: str,
    phone: str,
    confirm_phone: bool
):
    if not confirm_phone:
        raise ValueError(
            "Please confirm the phone number before booking."
        )

    phone_clean = normalize_phone(phone)

    if len(phone_clean) < 8 or len(phone_clean) > 15:
        raise ValueError("Please provide a valid phone number.")

    if not patient_name or not patient_name.strip():
        raise ValueError("Please provide the patient's name.")

    try:
        slot = (
            db.query(Slot)
            .filter(
                Slot.id == slot_id,
                Slot.is_booked == 0
            )
            .with_for_update()
            .first()
        )

        if not slot:
            raise ValueError(
                "That slot is no longer available. Please choose another slot."
            )

        # Prevent booking past dates or expired times.
        if not is_slot_in_future(slot):
            raise ValueError(
                "This appointment slot has expired. Please choose a future slot."
            )

        existing = (
            db.query(Appointment)
            .filter(
                Appointment.slot_id == slot_id,
                Appointment.status == "booked"
            )
            .first()
        )

        if existing:
            raise ValueError(
                "That slot is already booked. Please choose another slot."
            )

        appointment = Appointment(
            slot_id=slot.id,
            doctor_id=slot.doctor_id,
            patient_name=patient_name.strip(),
            phone=phone_clean,
            status="booked"
        )

        slot.is_booked = 1

        db.add(appointment)
        db.commit()
        db.refresh(appointment)

        return appointment

    except Exception:
        db.rollback()
        raise


def cancel_by_phone(
    db: Session,
    phone: str,
    confirm_phone: bool,
    appointment_id: int | None = None,
    confirm_cancellation: bool = False,
):
    if not isinstance(confirm_phone, bool) or not confirm_phone:
        raise ValueError(
            "Please confirm the phone number before cancellation."
        )

    phone_clean = normalize_phone(phone)

    if len(phone_clean) < 8 or len(phone_clean) > 15:
        raise ValueError("Please provide a valid phone number.")

    query = (
        db.query(Appointment)
        .filter(
            Appointment.phone == phone_clean,
            Appointment.status == "booked"
        )
    )

    appointments = query.order_by(Appointment.id).all()

    if not appointments:
        raise ValueError(
            "No active appointment was found for that phone number."
        )

    # Lookup is never cancellation, even when only one appointment matches.
    if appointment_id is None:
        return {
            "multiple": True,
            "selection_required": True,
            "appointments": appointments,
        }

    appointment = next(
        (a for a in appointments if a.id == appointment_id),
        None
    )

    if not appointment:
        raise ValueError(
            "Appointment ID does not match an active appointment "
            "for this phone number."
        )

    if not isinstance(confirm_cancellation, bool) or not confirm_cancellation:
        raise ValueError(
            "Please explicitly confirm cancellation of the selected appointment."
        )

    try:
        appointment.status = "cancelled"
        appointment.slot.is_booked = 0

        db.commit()
        db.refresh(appointment)

        return appointment

    except Exception:
        db.rollback()
        raise


def appointment_dict(a: Appointment):
    return {
        "id": a.id,
        "patient_name": a.patient_name,
        "phone": a.phone,
        "doctor": a.doctor.name,
        "specialty": a.doctor.specialty,
        "date": a.slot.date,
        "time": a.slot.time,
        "status": a.status
    }
