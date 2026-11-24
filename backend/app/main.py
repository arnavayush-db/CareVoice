
import asyncio
import logging
import re

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Appointment
from .schemas import BookingRequest, CancelRequest, AssistantRequest
from .seed import seed_database
from .services import (
    list_doctors,
    available_slots,
    book,
    cancel_by_phone,
    appointment_dict,
)
from .config import settings

from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequestHandler,
    SmallWebRTCRequest,
    SmallWebRTCPatchRequest,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.pipeline.runner import PipelineRunner

from pipecat_worker import run_pipeline

logger = logging.getLogger("uvicorn.error")


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

Base.metadata.create_all(bind=engine)

with next(get_db()) as db:
    seed_database(db)


# --------------------------------------------------
# CONVERSATION MEMORY
# --------------------------------------------------

conversations = {}


def get_session(conversation_id):
    if conversation_id not in conversations:
        conversations[conversation_id] = {
            "stage": "idle",
            "slots": [],
            "slot_id": None,
            "patient_name": None,
            "phone": None,
            "cancel_appointments": [],
            "cancel_appointment_id": None,
        }

    return conversations[conversation_id]


def reset_session(session):
    session.update({
        "stage": "idle",
        "slots": [],
        "slot_id": None,
        "patient_name": None,
        "phone": None,
        "cancel_appointments": [],
        "cancel_appointment_id": None,
    })


def normalize_phone(phone):
    return re.sub(r"\D", "", phone or "")


def is_yes(text):
    normalized = (text or "").lower().replace("'", "").replace("’", "")
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = " ".join(normalized.split())

    return normalized in {
        "yes", "yeah", "yep", "correct", "confirm",
        "confirmed", "right", "thats right",
        "that is right", "its correct", "that is correct",
        "yes its correct", "yes thats correct",
        "yes that is correct", "yeah correct", "yeah thats correct",
        "yeah that is correct", "yep correct", "yep thats correct",
        "yep that is correct", "haan", "han", "ha",
        "bilkul", "sahi hai", "haan sahi hai", "han sahi hai",
    }


def is_no(text):
    normalized = (text or "").lower().replace("'", "").replace("’", "")
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = " ".join(normalized.split())

    return normalized in {
        "no", "nope", "wrong", "thats wrong", "that is wrong",
        "incorrect", "its incorrect", "not correct",
        "no its incorrect", "no thats incorrect", "no that is incorrect",
        "nahi", "nahin", "galat", "galat hai",
    }


def find_doctor(text, db):
    doctors_list = list_doctors(db)
    text = text.lower()

    for doctor in doctors_list:
        if doctor.name.lower() in text:
            return doctor

    for doctor in doctors_list:
        if doctor.specialty.lower() in text:
            return doctor

    return None


def format_slots(found):
    return "; ".join(
        f"Option {i + 1}: {slot['doctor']}, "
        f"{slot['specialty']}, {slot['date']} at {slot['time']}"
        for i, slot in enumerate(found[:6])
    )


def format_appointments(found):
    return "; ".join(
        f"Option {i + 1}: Appointment ID {item['id']}, "
        f"{item['doctor']} on {item['date']} at {item['time']}"
        for i, item in enumerate(found)
    )


# --------------------------------------------------
# FASTAPI APPLICATION
# --------------------------------------------------

app = FastAPI(
    title="CareVoice API",
    version="1.0.0",
    description="Voice AI doctor appointment assistant",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_origin,
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "Authorization"],
)


# --------------------------------------------------
# WEBRTC INITIALIZATION
# --------------------------------------------------

webrtc_handler = SmallWebRTCRequestHandler()
voice_tasks = set()


async def webrtc_connection_callback(webrtc_connection):

    peer_id = webrtc_connection.pc_id
    logger.info("Starting Pipecat voice session for peer %s", peer_id)

    @webrtc_connection.event_handler("connected")
    async def on_peer_connected(connection):
        logger.info("WebRTC peer %s connected", connection.pc_id)

    @webrtc_connection.event_handler("disconnected")
    async def on_peer_disconnected(connection):
        logger.warning("WebRTC peer %s disconnected", connection.pc_id)

    @webrtc_connection.event_handler("closed")
    async def on_peer_closed(connection):
        logger.info("WebRTC peer %s closed", connection.pc_id)

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )
    logger.info("Created SmallWebRTCTransport for peer %s", peer_id)

    try:
        pipeline_task = await run_pipeline(transport)
        logger.info("Constructed Pipecat PipelineTask for peer %s", peer_id)
    except Exception:
        logger.exception("Could not construct Pipecat pipeline for peer %s", peer_id)
        raise

    runner = PipelineRunner()

    async def run_voice_session():
        try:
            logger.info("Starting Pipecat PipelineRunner for peer %s", peer_id)
            await runner.run(pipeline_task)
            if webrtc_connection.pc.connectionState != "closed":
                logger.error(
                    "Pipecat PipelineRunner exited while peer %s remained in state %s",
                    peer_id,
                    webrtc_connection.pc.connectionState,
                )
                await webrtc_connection.disconnect()
            else:
                logger.info("Pipecat voice session ended for peer %s", peer_id)
        except asyncio.CancelledError:
            logger.info("Pipecat voice task cancelled for peer %s", peer_id)
            raise
        except Exception:
            logger.exception("Pipecat voice session failed for peer %s", peer_id)
            if webrtc_connection.pc.connectionState != "closed":
                try:
                    await webrtc_connection.disconnect()
                except Exception:
                    logger.exception(
                        "Could not close failed WebRTC peer %s", peer_id
                    )

    background_task = asyncio.create_task(run_voice_session())
    voice_tasks.add(background_task)
    background_task.add_done_callback(voice_tasks.discard)


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "voice_mode": settings.voice_mode,
        "pipecat_configured": bool(
            settings.google_api_key
            and settings.elevenlabs_api_key
        ),
    }


# --------------------------------------------------
# DOCTORS
# --------------------------------------------------

@app.get("/api/doctors")
def doctors(db: Session = Depends(get_db)):
    return [
        {
            "id": d.id,
            "name": d.name,
            "specialty": d.specialty,
            "experience": d.experience,
            "fee": d.fee,
        }
        for d in list_doctors(db)
    ]


# --------------------------------------------------
# AVAILABLE SLOTS
# --------------------------------------------------

@app.get("/api/slots")
def slots(
    doctor_id: int | None = None,
    date: str | None = None,
    db: Session = Depends(get_db),
):
    return available_slots(db, doctor_id, date)


# --------------------------------------------------
# APPOINTMENTS
# --------------------------------------------------

@app.get("/api/appointments")
def appointments(db: Session = Depends(get_db)):
    rows = (
        db.query(Appointment)
        .order_by(Appointment.id.desc())
        .all()
    )

    return [appointment_dict(a) for a in rows]


@app.post("/api/appointments")
def create_appointment(
    payload: BookingRequest,
    db: Session = Depends(get_db),
):
    try:
        result = book(
            db,
            payload.slot_id,
            payload.patient_name,
            payload.phone,
            payload.confirm_phone,
        )

        return appointment_dict(result)

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------
# CANCELLATION ENDPOINT
# --------------------------------------------------

@app.post("/api/appointments/cancel")
def cancel_appointment(
    payload: CancelRequest,
    db: Session = Depends(get_db),
):
    try:
        if not payload.confirm_phone:
            raise ValueError("Please confirm the phone number before cancellation.")

        # This first call is lookup only. A caller must explicitly select an
        # appointment even when the phone number matches only one booking.
        lookup = cancel_by_phone(
            db=db,
            phone=payload.phone,
            confirm_phone=True,
        )
        found = lookup["appointments"]
        formatted = [appointment_dict(a) for a in found]

        if payload.appointment_id is None:
            return {
                "selection_required": True,
                "appointments": formatted,
            }

        selected = next(
            (item for item in found if item.id == payload.appointment_id),
            None,
        )
        if selected is None:
            raise ValueError(
                "Appointment ID does not match an active appointment for this phone number."
            )

        if not payload.confirm_cancellation:
            return {
                "confirmation_required": True,
                "appointment": appointment_dict(selected),
            }

        result = cancel_by_phone(
            db=db,
            phone=payload.phone,
            confirm_phone=True,
            appointment_id=payload.appointment_id,
            confirm_cancellation=True,
        )

        return {
            "success": True,
            "message": "Appointment cancelled successfully.",
            "appointment": appointment_dict(result),
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------
# CONVERSATIONAL ASSISTANT
# --------------------------------------------------

@app.post("/api/assistant")
def assistant(
    payload: AssistantRequest,
    db: Session = Depends(get_db),
):
    text = payload.message.strip()
    low = text.lower()

    conversation_id = payload.conversation_id or "default"
    session = get_session(conversation_id)

    if not text:
        return {
            "reply": "Please tell me how I can help you.",
            "action": "greeting",
        }

    # --------------------------------------------------
    # CANCELLATION STEP 1: COLLECT PHONE NUMBER
    # --------------------------------------------------

    if session["stage"] == "cancel_awaiting_phone":

        phone = normalize_phone(text)

        if len(phone) < 8 or len(phone) > 15:
            return {
                "reply": (
                    "That phone number doesn't look valid. "
                    "Please say the registered phone number again."
                ),
                "action": "cancel_collect_phone",
            }

        session["phone"] = phone
        session["stage"] = "cancel_awaiting_phone_confirmation"

        return {
            "reply": (
                f"I heard your phone number as {' '.join(phone)}. "
                "Is this correct? Please say yes or no."
            ),
            "action": "cancel_confirm_phone",
        }

    # --------------------------------------------------
    # CANCELLATION STEP 2: CONFIRM PHONE NUMBER
    # --------------------------------------------------

    if session["stage"] == "cancel_awaiting_phone_confirmation":

        if is_no(low):
            session["phone"] = None
            session["stage"] = "cancel_awaiting_phone"

            return {
                "reply": "No problem. Please tell me the correct phone number.",
                "action": "cancel_collect_phone",
            }

        if not is_yes(low):
            return {
                "reply": (
                    "Please say yes if the number is correct, "
                    "or no to enter it again."
                ),
                "action": "cancel_confirm_phone",
            }

        try:
            lookup = cancel_by_phone(
                db=db,
                phone=session["phone"],
                confirm_phone=True,
            )
            found = [appointment_dict(a) for a in lookup["appointments"]]
            session["cancel_appointments"] = found
            session["stage"] = "cancel_awaiting_selection"

            if len(found) == 1:
                prompt = (
                    "I found one active appointment. "
                    f"{format_appointments(found)}. "
                    "Please select option 1 before I ask for final confirmation."
                )
            else:
                prompt = (
                    "I found multiple active appointments. "
                    f"{format_appointments(found)}. "
                    "Which appointment would you like to cancel? "
                    "Please say the option number."
                )

            return {
                "reply": prompt,
                "action": "cancel_select_appointment",
                "appointments": found,
            }

        except ValueError as exc:
            reset_session(session)

            return {
                "reply": (
                    f"I couldn't cancel the appointment: {str(exc)} "
                    "Please check the registered phone number."
                ),
                "action": "cancellation_error",
            }

    # --------------------------------------------------
    # CANCELLATION STEP 3: SELECT APPOINTMENT
    # --------------------------------------------------

    if session["stage"] == "cancel_awaiting_selection":

        match = re.search(r"\b(\d+)\b", low)

        selected = None

        if match:
            number = int(match.group(1))

            if 1 <= number <= len(session["cancel_appointments"]):
                selected = session["cancel_appointments"][number - 1]

        if not selected:
            return {
                "reply": (
                    "I couldn't identify that appointment. "
                    "Please say a valid option number."
                ),
                "action": "cancel_select_appointment",
                "appointments": session["cancel_appointments"],
            }

        session["cancel_appointment_id"] = selected["id"]
        session["stage"] = "cancel_awaiting_final_confirmation"

        return {
            "reply": (
                f"You selected the appointment with {selected['doctor']} "
                f"on {selected['date']} at {selected['time']}. "
                "Are you sure you want to cancel this appointment?"
            ),
            "action": "cancel_final_confirmation",
            "appointment": selected,
        }

    # --------------------------------------------------
    # CANCELLATION STEP 4: FINAL CONFIRMATION
    # --------------------------------------------------

    if session["stage"] == "cancel_awaiting_final_confirmation":

        if is_no(low):
            reset_session(session)

            return {
                "reply": "Okay. Your appointment has not been cancelled.",
                "action": "cancellation_aborted",
            }

        if not is_yes(low):
            return {
                "reply": (
                    "Please say yes to confirm cancellation "
                    "or no to keep your appointment."
                ),
                "action": "cancel_final_confirmation",
            }

        try:
            result = cancel_by_phone(
                db=db,
                phone=session["phone"],
                confirm_phone=True,
                appointment_id=session["cancel_appointment_id"],
                confirm_cancellation=True,
            )

            response = appointment_dict(result)
            reset_session(session)

            return {
                "reply": (
                    "Your selected appointment has been cancelled successfully. "
                    f"The appointment with {response['doctor']} "
                    f"on {response['date']} at {response['time']} "
                    "has been cancelled."
                ),
                "action": "cancellation_success",
                "appointment": response,
            }

        except ValueError as exc:
            reset_session(session)

            return {
                "reply": (
                    f"Cancellation failed: {str(exc)}. "
                    "Please try again."
                ),
                "action": "cancellation_error",
            }

    # --------------------------------------------------
    # BOOKING STEP 1: COLLECT DOCTOR
    # --------------------------------------------------

    if session["stage"] == "awaiting_doctor":

        doctor = find_doctor(low, db)

        if not doctor:
            return {
                "reply": (
                    "I couldn't identify that specialty or doctor. "
                    "Please tell me the doctor's name or specialty."
                ),
                "action": "collect_doctor",
            }

        found = available_slots(db, doctor.id)

        if not found:
            return {
                "reply": (
                    f"Sorry, no available slots were found for "
                    f"{doctor.name}. Would you like another doctor?"
                ),
                "action": "availability",
                "slots": [],
            }

        session["slots"] = found[:12]
        session["stage"] = "awaiting_slot"

        return {
            "reply": (
                f"I found these available appointments for "
                f"{doctor.name}: {format_slots(found)}. "
                "Please tell me which option you prefer."
            ),
            "action": "availability",
            "slots": found[:12],
        }

    # --------------------------------------------------
    # BOOKING STEP 2: SELECT SLOT
    # --------------------------------------------------

    if session["stage"] == "awaiting_slot":

        match = re.search(r"\b(?:option|number|slot)?\s*(\d+)\b", low)

        selected = None

        if match:
            number = int(match.group(1))

            if 1 <= number <= len(session["slots"]):
                selected = session["slots"][number - 1]

        if not selected:
            for slot in session["slots"]:
                if (
                    slot["time"].lower() in low
                    or slot["date"].lower() in low
                ):
                    selected = slot
                    break

        if not selected:
            return {
                "reply": (
                    "I couldn't identify that option. "
                    "Please say an option number, such as option 1."
                ),
                "action": "select_slot",
                "slots": session["slots"],
            }

        session["slot_id"] = selected["id"]
        session["stage"] = "awaiting_name"

        return {
            "reply": (
                f"You selected {selected['doctor']} on "
                f"{selected['date']} at {selected['time']}. "
                "May I have the patient's full name?"
            ),
            "action": "collect_name",
            "selected_slot": selected,
        }

    # --------------------------------------------------
    # BOOKING STEP 3: COLLECT NAME
    # --------------------------------------------------

    if session["stage"] == "awaiting_name":

        if len(text) < 2 or len(text) > 100:
            return {
                "reply": "Please tell me the patient's full name.",
                "action": "collect_name",
            }

        session["patient_name"] = text
        session["stage"] = "awaiting_phone"

        return {
            "reply": (
                f"Thank you, {text}. "
                "Please tell me the phone number associated "
                "with this appointment."
            ),
            "action": "collect_phone",
        }

    # --------------------------------------------------
    # BOOKING STEP 4: COLLECT PHONE
    # --------------------------------------------------

    if session["stage"] == "awaiting_phone":

        phone = normalize_phone(text)

        if len(phone) < 8 or len(phone) > 15:
            return {
                "reply": (
                    "That phone number doesn't look valid. "
                    "Please say the number again."
                ),
                "action": "collect_phone",
            }

        session["phone"] = phone
        session["stage"] = "awaiting_phone_confirmation"

        return {
            "reply": (
                f"I heard your phone number as {' '.join(phone)}. "
                "Is this correct? Please say yes or no."
            ),
            "action": "confirm_phone",
        }

    # --------------------------------------------------
    # BOOKING STEP 5: CONFIRM PHONE
    # --------------------------------------------------

    if session["stage"] == "awaiting_phone_confirmation":

        if is_no(low):
            session["phone"] = None
            session["stage"] = "awaiting_phone"

            return {
                "reply": (
                    "No problem. Please tell me the correct phone number."
                ),
                "action": "collect_phone",
            }

        if not is_yes(low):
            return {
                "reply": (
                    "Please clearly say yes if the number is correct, "
                    "or no if you want to enter it again."
                ),
                "action": "confirm_phone",
            }

        try:
            result = book(
                db,
                session["slot_id"],
                session["patient_name"],
                session["phone"],
                True,
            )

            response = appointment_dict(result)
            reset_session(session)

            return {
                "reply": (
                    "Your appointment has been booked successfully. "
                    f"The appointment is with {response['doctor']} "
                    f"on {response['date']} at {response['time']}. "
                    f"Your appointment ID is {response['id']}."
                ),
                "action": "booking_success",
                "appointment": response,
            }

        except ValueError as exc:
            reset_session(session)

            return {
                "reply": (
                    f"I couldn't complete the booking: {str(exc)} "
                    "Please start a new booking."
                ),
                "action": "booking_error",
            }

    # --------------------------------------------------
    # INTENTS
    # --------------------------------------------------

    booking_intent = any(
        word in low
        for word in ("book", "booking", "schedule", "reserve")
    )

    availability_intent = any(
        word in low
        for word in (
            "available", "availability", "slots",
            "free time", "appointment"
        )
    )

    # --------------------------------------------------
    # START CANCELLATION
    # --------------------------------------------------

    if "cancel" in low:

        reset_session(session)
        session["stage"] = "cancel_awaiting_phone"

        return {
            "reply": (
                "I can help you cancel an appointment. "
                "Please tell me the registered phone number "
                "used when booking."
            ),
            "action": "cancel_collect_phone",
        }

    # --------------------------------------------------
    # START BOOKING
    # --------------------------------------------------

    if booking_intent:

        doctor = find_doctor(low, db)

        if not doctor:
            session["stage"] = "awaiting_doctor"

            return {
                "reply": (
                    "Sure! Which doctor or medical specialty "
                    "would you like to book? For example, cardiologist "
                    "or dermatologist."
                ),
                "action": "collect_doctor",
            }

        found = available_slots(db, doctor.id)

        if not found:
            return {
                "reply": (
                    f"Sorry, no available slots were found for "
                    f"{doctor.name}. Would you like another doctor?"
                ),
                "action": "availability",
                "slots": [],
            }

        session["slots"] = found[:12]
        session["stage"] = "awaiting_slot"

        return {
            "reply": (
                f"Sure! Here are the available appointments "
                f"for {doctor.name}: {format_slots(found)}. "
                "Which option would you like?"
            ),
            "action": "availability",
            "slots": found[:12],
        }

    # --------------------------------------------------
    # CHECK AVAILABILITY
    # --------------------------------------------------

    if availability_intent:

        doctor = find_doctor(low, db)

        found = available_slots(
            db,
            doctor.id if doctor else None,
        )

        if not found:
            return {
                "reply": "I couldn't find any available appointments.",
                "action": "availability",
                "slots": [],
            }

        return {
            "reply": (
                "Here are some available appointments: "
                f"{format_slots(found)}. "
                "Would you like to book one?"
            ),
            "action": "availability",
            "slots": found[:12],
        }

    # --------------------------------------------------
    # GREETING
    # --------------------------------------------------

    if any(word in low for word in ("hello", "hi", "hey", "namaste")):
        return {
            "reply": (
                "Hello! I'm CareVoice, your doctor appointment assistant. "
                "I can check availability, book appointments, "
                "or help with cancellations. How can I help?"
            ),
            "action": "greeting",
        }

    return {
        "reply": (
            "I can help you check doctor availability, "
            "book an appointment, or cancel an existing appointment. "
            "What would you like to do?"
        ),
        "action": "general",
    }


# --------------------------------------------------
# WEBRTC OFFER ENDPOINT
# --------------------------------------------------

@app.post("/api/offer")
async def webrtc_offer(request: SmallWebRTCRequest):
    logger.info(
        "Received WebRTC %s offer (%d SDP characters, audio=%s, datachannel=%s, candidates=%d)",
        request.type,
        len(request.sdp),
        "m=audio" in request.sdp,
        "m=application" in request.sdp,
        request.sdp.count("a=candidate:"),
    )
    try:
        answer = await webrtc_handler.handle_web_request(
            request,
            webrtc_connection_callback,
        )

        if answer is None:
            raise HTTPException(
                status_code=500,
                detail="WebRTC connection did not produce an answer.",
            )

        answer_sdp = answer.get("sdp", "")
        logger.info(
            "Created WebRTC answer for peer %s (audio=%s, datachannel=%s, candidates=%d)",
            answer.get("pc_id"),
            "m=audio" in answer_sdp,
            "m=application" in answer_sdp,
            answer_sdp.count("a=candidate:"),
        )
        return answer

    except HTTPException as exc:
        logger.warning("WebRTC offer rejected: %s", exc.detail)
        raise

    except Exception as exc:
        logger.exception("WebRTC offer handling failed")
        raise HTTPException(
            status_code=500,
            detail=f"WebRTC offer failed: {str(exc)}",
        )


# --------------------------------------------------
# WEBRTC PATCH ENDPOINT
# --------------------------------------------------

@app.patch("/api/offer")
async def webrtc_patch(request: SmallWebRTCPatchRequest):
    try:
        await webrtc_handler.handle_patch_request(request)

        return {
            "status": "success",
            "message": "WebRTC connection updated.",
        }

    except Exception as exc:
        logger.exception("WebRTC ICE candidate patch failed for peer %s", request.pc_id)
        raise HTTPException(
            status_code=500,
            detail=f"WebRTC patch failed: {str(exc)}",
        )


# --------------------------------------------------
# VOICE STATUS
# --------------------------------------------------

@app.get("/api/voice/status")
def voice_status():
    return {
        "mode": "live",
        "message": "CareVoice WebRTC endpoint is configured.",
        "pipecat_configured": bool(
            settings.google_api_key
            and settings.elevenlabs_api_key
        ),
    }


# --------------------------------------------------
# APPLICATION SHUTDOWN
# --------------------------------------------------

@app.on_event("shutdown")
async def shutdown_event():
    await webrtc_handler.close()
