
"""Appointment tools for the CareVoice Pipecat assistant."""

import re

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Appointment
from app.services import (
    list_doctors,
    available_slots,
    book,
    cancel_by_phone,
    appointment_dict,
)


def normalize_phone(phone):
    return re.sub(r"\D", "", str(phone))


async def check_doctor_availability(
    doctor_name: str = "",
    specialty: str = "",
    date: str = "",
):
    db: Session = SessionLocal()

    try:
        doctors = list_doctors(db)

        doctor = next(
            (
                d for d in doctors
                if (
                    doctor_name
                    and doctor_name.lower() in d.name.lower()
                )
                or (
                    specialty
                    and specialty.lower() in d.specialty.lower()
                )
            ),
            None,
        )

        slots = available_slots(
            db,
            doctor.id if doctor else None,
            date or None,
        )

        return {
            "success": True,
            "doctors": [
                {
                    "id": d.id,
                    "name": d.name,
                    "specialty": d.specialty,
                    "experience": d.experience,
                    "fee": d.fee,
                }
                for d in doctors
            ],
            "available_slots": slots[:12],
            "message": (
                "Available appointment slots found."
                if slots
                else "No available slots found."
            ),
        }

    except Exception as exc:
        return {
            "success": False,
            "error": f"Availability check failed: {str(exc)}",
        }

    finally:
        db.close()


async def book_appointment(
    slot_id: int,
    patient_name: str,
    phone: str,
    confirm_phone: bool,
):
    db: Session = SessionLocal()

    try:
        if not confirm_phone:
            return {
                "success": False,
                "error": "Phone number confirmation is required.",
            }

        appointment = book(
            db=db,
            slot_id=slot_id,
            patient_name=patient_name,
            phone=normalize_phone(phone),
            confirm_phone=True,
        )

        return {
            "success": True,
            "message": "Appointment booked successfully.",
            "appointment": appointment_dict(appointment),
        }

    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    except Exception as exc:
        db.rollback()
        return {
            "success": False,
            "error": f"Booking failed: {str(exc)}",
        }

    finally:
        db.close()


async def cancel_appointment(
    phone: str,
    confirm_phone: bool,
    appointment_id: int | None = None,
    confirm_cancellation: bool = False,
):
    """
    Without appointment_id: list active appointments only.
    With appointment_id: cancel only that selected appointment.
    """

    db: Session = SessionLocal()

    try:
        if not isinstance(confirm_phone, bool) or not confirm_phone:
            return {
                "success": False,
                "error": "Phone number confirmation is required.",
            }

        normalized_phone = normalize_phone(phone)

        if len(normalized_phone) < 8:
            return {
                "success": False,
                "error": "Invalid phone number.",
            }

        # PREVIEW: Never cancel during appointment lookup.
        if appointment_id is None:
            appointments = (
                db.query(Appointment)
                .filter(
                    Appointment.phone == normalized_phone,
                    Appointment.status == "booked",
                )
                .order_by(Appointment.id.asc())
                .all()
            )

            if not appointments:
                return {
                    "success": False,
                    "message": "No active appointments found.",
                    "appointments": [],
                }

            return {
                "success": False,
                "selection_required": True,
                "message": (
                    "Active appointments found. "
                    "Ask the caller to select one. "
                    "Nothing has been cancelled."
                ),
                "appointments": [
                    appointment_dict(a) for a in appointments
                ],
            }

        # EXECUTION: Cancel only the explicitly selected appointment.
        if not isinstance(confirm_cancellation, bool) or not confirm_cancellation:
            return {
                "success": False,
                "confirmation_required": True,
                "error": "Explicit cancellation confirmation is required.",
            }

        result = cancel_by_phone(
            db=db,
            phone=normalized_phone,
            confirm_phone=True,
            appointment_id=appointment_id,
            confirm_cancellation=True,
        )

        if isinstance(result, dict) and result.get("multiple"):
            return {
                "success": False,
                "selection_required": True,
                "message": "Please select one appointment.",
                "appointments": [
                    appointment_dict(a)
                    for a in result["appointments"]
                ],
            }

        return {
            "success": True,
            "message": "Appointment cancelled successfully.",
            "appointment": appointment_dict(result),
        }

    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    except Exception as exc:
        db.rollback()
        return {
            "success": False,
            "error": f"Cancellation failed: {str(exc)}",
        }

    finally:
        db.close()
