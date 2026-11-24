
"""
CareVoice - Pipecat Voice Doctor Appointment Assistant

Pipeline:
Browser Audio
    -> Silero VAD
    -> ElevenLabs STT
    -> Gemini LLM + Appointment Tools
    -> ElevenLabs TTS
    -> Browser Audio
"""

import os
import re
import logging

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("uvicorn.error")


# =========================================================
# 1. SILERO VAD
# =========================================================

def build_vad_processor():
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor

    return VADProcessor(
        vad_analyzer=SileroVADAnalyzer()
    )


# =========================================================
# 2. MESSAGE / CONFIRMATION HELPERS
# =========================================================

def normalize_phone(phone):
    return re.sub(r"\D", "", str(phone))


def get_message_value(message, key):
    if isinstance(message, dict):
        return message.get(key)

    return getattr(message, key, None)


def get_last_user_message(context):
    messages = context.messages or []

    for message in reversed(messages):
        role = get_message_value(message, "role")

        if role != "user":
            continue

        content = get_message_value(message, "content")

        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            parts = []

            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                else:
                    parts.append(str(item))

            return " ".join(parts).strip()

        return str(content or "").strip()

    return ""


def count_user_messages(context):
    messages = context.messages or []

    return sum(
        1
        for message in messages
        if get_message_value(message, "role") == "user"
    )


def normalize_confirmation_text(text):
    normalized = re.sub(
        r"[^a-zA-Z0-9 ]",
        "",
        str(text).lower()
    )

    return " ".join(normalized.split())


def is_explicit_confirmation(text):
    accepted = {
        "yes",
        "yeah",
        "yep",
        "yes correct",
        "yes thats correct",
        "yes that is correct",
        "that is correct",
        "thats correct",
        "yes its correct",
        "yes it is correct",
        "yeah correct",
        "yep correct",
        "correct",
        "thats right",
        "that is right",
        "haan",
        "haan sahi hai",
        "sahi hai",
        "ji haan",
        "bilkul sahi",
        "bilkul",
    }

    return normalize_confirmation_text(text) in accepted


def is_explicit_rejection(text):
    rejected = {
        "no",
        "nope",
        "no its incorrect",
        "no it is incorrect",
        "no cancel",
        "no dont cancel",
        "no do not cancel",
        "thats wrong",
        "that is wrong",
        "nahi",
        "galat hai",
        "nahi chahiye",
        "mat karo",
        "cancel mat karo",
    }

    return normalize_confirmation_text(text) in rejected


# =========================================================
# 3. MAIN PIPECAT PIPELINE
# =========================================================

async def run_pipeline(transport):

    # -----------------------------------------------------
    # Check API credentials
    # -----------------------------------------------------

    required_keys = [
        "GOOGLE_API_KEY",
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID",
    ]

    for key in required_keys:
        if not os.getenv(key):
            raise RuntimeError(
                f"{key} missing in backend/.env"
            )

    # -----------------------------------------------------
    # Pipecat imports
    # -----------------------------------------------------

    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineTask
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        OutputTransportMessageFrame,
        TranscriptionFrame,
    )
    from pipecat.processors.frame_processor import (
        FrameDirection,
        FrameProcessor,
    )

    from pipecat.services.google.llm import GoogleLLMService

    from pipecat.services.elevenlabs.stt import (
        ElevenLabsRealtimeSTTService
    )

    from pipecat.services.elevenlabs.tts import (
        ElevenLabsTTSService
    )

    from pipecat.adapters.llm.schemas.function_schema import (
        FunctionSchema
    )

    from pipecat.adapters.llm.schemas.tools_schema import (
        ToolsSchema
    )

    from pipecat.processors.aggregators.llm_context import (
        LLMContext
    )

    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair
    )
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMUserAggregatorParams
    )
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    from pipecat.services.llm_service import FunctionCallParams

    # -----------------------------------------------------
    # Import appointment functions
    # -----------------------------------------------------

    try:
        from .voice_tools import (
            check_doctor_availability,
            book_appointment,
            cancel_appointment,
        )

    except ImportError:
        from voice_tools import (
            check_doctor_availability,
            book_appointment,
            cancel_appointment,
        )

    # -----------------------------------------------------
    # Session-specific pending confirmations
    # -----------------------------------------------------

    pending_bookings = {}
    pending_cancellations = {}
    output_transport = transport.output()

    async def send_appointment_update(action):
        try:
            await output_transport.send_message(
                OutputTransportMessageFrame({
                    "type": "appointment_update",
                    "action": action,
                })
            )
        except Exception:
            # Dashboard notifications are independent of the voice pipeline.
            logger.exception("Could not relay appointment update (%s)", action)

    # =====================================================
    # 4. AVAILABILITY HANDLER
    # =====================================================

    async def availability_handler(params: FunctionCallParams):

        try:
            args = params.arguments

            result = await check_doctor_availability(
                doctor_name=args.get("doctor_name", ""),
                specialty=args.get("specialty", ""),
                date=args.get("date", ""),
            )

        except Exception as exc:
            logger.exception("Availability tool failed")

            result = {
                "success": False,
                "error": str(exc),
            }

        await params.result_callback(result)

    # =====================================================
    # 5. BOOKING HANDLER
    # =====================================================

    async def booking_handler(params: FunctionCallParams):

        args = params.arguments

        try:
            slot_id = int(args["slot_id"])
            patient_name = str(args["patient_name"]).strip()
            phone = normalize_phone(args["phone"])
            confirm_phone = args["confirm_phone"]

            if not isinstance(confirm_phone, bool):
                raise ValueError(
                    "Phone confirmation must be true or false."
                )

            if len(phone) < 8:
                raise ValueError("Invalid phone number.")

            if not patient_name:
                raise ValueError("Patient name is required.")

            # First call: prepare phone confirmation.
            if confirm_phone is False:

                pending_bookings[phone] = {
                    "slot_id": slot_id,
                    "patient_name": patient_name,
                    "user_message_count": count_user_messages(
                        params.context
                    ),
                }

                result = {
                    "success": False,
                    "confirmation_required": True,
                    "message": (
                        "Read the patient's complete phone number "
                        "digit by digit. Ask whether it is correct. "
                        "Do not book yet."
                    ),
                }

                await params.result_callback(result)
                return

            # Second call: verify pending request.
            pending = pending_bookings.get(phone)

            if not pending:
                result = {
                    "success": False,
                    "error": (
                        "No pending booking confirmation exists. "
                        "Ask the caller to confirm the number first."
                    ),
                }

                await params.result_callback(result)
                return

            if (
                pending["slot_id"] != slot_id
                or pending["patient_name"] != patient_name
            ):
                pending_bookings.pop(phone, None)

                result = {
                    "success": False,
                    "error": (
                        "Booking details changed. "
                        "Please repeat the confirmation process."
                    ),
                }

                await params.result_callback(result)
                return

            current_user_count = count_user_messages(
                params.context
            )

            if current_user_count <= pending["user_message_count"]:
                result = {
                    "success": False,
                    "confirmation_required": True,
                    "error": (
                        "A new caller confirmation is required "
                        "after reading back the phone number."
                    ),
                }

                await params.result_callback(result)
                return

            last_user_text = get_last_user_message(
                params.context
            )

            if not is_explicit_confirmation(last_user_text):
                result = {
                    "success": False,
                    "confirmation_required": True,
                    "error": (
                        "The latest caller response is not an "
                        "explicit confirmation. Ask again."
                    ),
                }

                await params.result_callback(result)
                return

            # Confirmation passed. Execute actual booking.
            result = await book_appointment(
                slot_id=slot_id,
                patient_name=patient_name,
                phone=phone,
                confirm_phone=True,
            )

            if result.get("success"):
                await send_appointment_update("booked")

            pending_bookings.pop(phone, None)

        except Exception as exc:
            logger.exception("Booking tool failed")

            result = {
                "success": False,
                "error": str(exc),
            }

        await params.result_callback(result)

    # =====================================================
    # 6. CANCELLATION HANDLER
    # =====================================================

    async def cancellation_handler(params: FunctionCallParams):

        args = params.arguments

        try:
            phone = normalize_phone(args["phone"])
            confirm_phone = args["confirm_phone"]

            appointment_id = args.get("appointment_id")
            confirm_cancellation = args.get(
                "confirm_cancellation", False
            )

            if not isinstance(confirm_phone, bool):
                raise ValueError(
                    "Phone confirmation must be true or false."
                )

            if not isinstance(confirm_cancellation, bool):
                raise ValueError(
                    "Cancellation confirmation must be true or false."
                )

            if len(phone) < 8:
                raise ValueError("Invalid phone number.")

            # -------------------------------------------------
            # STAGE 1: Ask caller to confirm registered phone.
            # -------------------------------------------------

            if confirm_phone is False:

                pending_cancellations[phone] = {
                    "stage": "phone_confirmation",
                    "user_message_count": count_user_messages(
                        params.context
                    ),
                    "appointments": [],
                    "selected_appointment_id": None,
                }

                result = {
                    "success": False,
                    "confirmation_required": True,
                    "message": (
                        "Read the complete registered phone number "
                        "digit by digit. Ask the caller to confirm it. "
                        "Do not cancel anything."
                    ),
                }

                await params.result_callback(result)
                return

            pending = pending_cancellations.get(phone)

            if not pending:
                result = {
                    "success": False,
                    "error": (
                        "No pending cancellation session exists. "
                        "Start the phone confirmation process again."
                    ),
                }

                await params.result_callback(result)
                return

            current_user_count = count_user_messages(
                params.context
            )

            last_user_text = get_last_user_message(
                params.context
            )

            # -------------------------------------------------
            # STAGE 2: Verify phone confirmation.
            # -------------------------------------------------

            if pending["stage"] == "phone_confirmation":

                if current_user_count <= pending["user_message_count"]:
                    result = {
                        "success": False,
                        "confirmation_required": True,
                        "error": (
                            "A new caller response is required "
                            "after reading back the phone number."
                        ),
                    }

                    await params.result_callback(result)
                    return

                if not is_explicit_confirmation(last_user_text):
                    result = {
                        "success": False,
                        "confirmation_required": True,
                        "error": (
                            "The caller has not explicitly confirmed "
                            "the phone number."
                        ),
                    }

                    await params.result_callback(result)
                    return

                # Only LIST appointments here. Do not cancel.
                lookup = await cancel_appointment(
                    phone=phone,
                    confirm_phone=True,
                    appointment_id=None,
                )

                appointments = lookup.get("appointments", [])

                if not appointments:
                    pending_cancellations.pop(phone, None)

                    result = {
                        "success": False,
                        "message": (
                            "No active appointments were found "
                            "for this phone number."
                        ),
                        "appointments": [],
                    }

                    await params.result_callback(result)
                    return

                pending["appointments"] = appointments

                # Exactly one appointment: select it automatically,
                # but still ask for a separate final confirmation.
                if len(appointments) == 1:

                    selected = appointments[0]

                    pending["stage"] = "final_confirmation"
                    pending["selected_appointment_id"] = int(
                        selected["id"]
                    )
                    pending["user_message_count"] = current_user_count

                    result = {
                        "success": False,
                        "confirmation_required": True,
                        "stage": "final_confirmation",
                        "appointment": selected,
                        "message": (
                            "One active appointment was found. "
                            "Read its doctor, date and time to the caller. "
                            "Ask whether they want to cancel this exact "
                            "appointment. Do not cancel yet."
                        ),
                    }

                    await params.result_callback(result)
                    return

                # Multiple appointments: caller must select one.
                pending["stage"] = "selection"
                pending["user_message_count"] = current_user_count

                result = {
                    "success": False,
                    "selection_required": True,
                    "stage": "selection",
                    "appointments": appointments,
                    "message": (
                        "Multiple active appointments were found. "
                        "Read out the appointment IDs, doctors, dates "
                        "and times. Ask the caller which one to cancel. "
                        "Do not cancel anything yet."
                    ),
                }

                await params.result_callback(result)
                return

            # -------------------------------------------------
            # STAGE 3: Select one appointment.
            # -------------------------------------------------

            if pending["stage"] == "selection":

                if current_user_count <= pending["user_message_count"]:
                    result = {
                        "success": False,
                        "selection_required": True,
                        "error": (
                            "Wait for the caller to select an "
                            "appointment before proceeding."
                        ),
                    }

                    await params.result_callback(result)
                    return

                if appointment_id is None:
                    result = {
                        "success": False,
                        "selection_required": True,
                        "error": (
                            "The caller must select an appointment ID."
                        ),
                    }

                    await params.result_callback(result)
                    return

                appointment_id = int(appointment_id)

                selected = next(
                    (
                        item
                        for item in pending["appointments"]
                        if int(item["id"]) == appointment_id
                    ),
                    None,
                )

                if selected is None:
                    result = {
                        "success": False,
                        "selection_required": True,
                        "error": (
                            "That appointment does not belong to "
                            "the caller's confirmed appointment list. "
                            "Ask the caller to choose a listed ID."
                        ),
                    }

                    await params.result_callback(result)
                    return

                # Store selection; no cancellation yet.
                pending["selected_appointment_id"] = appointment_id
                pending["stage"] = "final_confirmation"
                pending["user_message_count"] = current_user_count

                result = {
                    "success": False,
                    "confirmation_required": True,
                    "stage": "final_confirmation",
                    "appointment": selected,
                    "message": (
                        "Appointment selected. Read its doctor, date "
                        "and time back to the caller. Ask for a separate "
                        "explicit yes before cancelling. Do not cancel yet."
                    ),
                }

                await params.result_callback(result)
                return

            # -------------------------------------------------
            # STAGE 4: Final confirmation before cancellation.
            # -------------------------------------------------

            if pending["stage"] == "final_confirmation":

                selected_id = pending["selected_appointment_id"]

                if appointment_id is None:
                    result = {
                        "success": False,
                        "confirmation_required": True,
                        "error": (
                            "The selected appointment ID is required."
                        ),
                    }

                    await params.result_callback(result)
                    return

                if int(appointment_id) != int(selected_id):
                    result = {
                        "success": False,
                        "confirmation_required": True,
                        "error": (
                            "The appointment ID does not match "
                            "the selected appointment."
                        ),
                    }

                    await params.result_callback(result)
                    return

                if current_user_count <= pending["user_message_count"]:
                    result = {
                        "success": False,
                        "confirmation_required": True,
                        "error": (
                            "A new caller response is required "
                            "after the appointment details were read back."
                        ),
                    }

                    await params.result_callback(result)
                    return

                if is_explicit_rejection(last_user_text):
                    pending_cancellations.pop(phone, None)

                    result = {
                        "success": False,
                        "cancelled": False,
                        "message": (
                            "Cancellation stopped. "
                            "The appointment remains active."
                        ),
                    }

                    await params.result_callback(result)
                    return

                if not confirm_cancellation or not is_explicit_confirmation(
                    last_user_text
                ):
                    result = {
                        "success": False,
                        "confirmation_required": True,
                        "error": (
                            "The caller has not explicitly confirmed "
                            "cancellation of the selected appointment. "
                            "Ask again. Do not cancel."
                        ),
                    }

                    await params.result_callback(result)
                    return

                # FINAL CONFIRMATION PASSED.
                # Now and only now execute cancellation.
                result = await cancel_appointment(
                    phone=phone,
                    confirm_phone=True,
                    appointment_id=int(selected_id),
                    confirm_cancellation=True,
                )

                if result.get("success"):
                    pending_cancellations.pop(phone, None)
                    await send_appointment_update("cancelled")

                return_result = result

                await params.result_callback(return_result)
                return

            result = {
                "success": False,
                "error": "Unknown cancellation session stage.",
            }

        except Exception as exc:
            logger.exception("Cancellation tool failed")

            result = {
                "success": False,
                "error": str(exc),
            }

        await params.result_callback(result)

    # =====================================================
    # 7. DEFINE GEMINI FUNCTION SCHEMAS
    # =====================================================

    availability_schema = FunctionSchema(
        name="check_doctor_availability",
        description=(
            "Find doctors and available appointment slots "
            "by doctor name, specialty or date."
        ),
        properties={
            "doctor_name": {
                "type": "string",
                "description": "Doctor name, if specified.",
            },
            "specialty": {
                "type": "string",
                "description": "Medical specialty, if specified.",
            },
            "date": {
                "type": "string",
                "description": "Date in YYYY-MM-DD format.",
            },
        },
        required=[],
        handler=availability_handler,
    )

    booking_schema = FunctionSchema(
        name="book_appointment",
        description=(
            "Book an available doctor appointment. First call with "
            "confirm_phone=false to request confirmation. Call again "
            "with true only after the caller explicitly confirms "
            "the phone number in a new spoken response."
        ),
        properties={
            "slot_id": {
                "type": "integer",
                "description": "Available appointment slot ID.",
            },
            "patient_name": {
                "type": "string",
                "description": "Patient's full name.",
            },
            "phone": {
                "type": "string",
                "description": "Patient's phone number.",
            },
            "confirm_phone": {
                "type": "boolean",
                "description": (
                    "False for initial confirmation; true only "
                    "after explicit caller confirmation."
                ),
            },
        },
        required=[
            "slot_id",
            "patient_name",
            "phone",
            "confirm_phone",
        ],
        handler=booking_handler,
    )

    cancellation_schema = FunctionSchema(
        name="cancel_appointment",
        description=(
            "Safely cancel one appointment using the registered phone. "
            "First call with confirm_phone=false. After the caller "
            "confirms the phone, call with confirm_phone=true and no "
            "appointment_id to list active appointments. If there are "
            "multiple appointments, ask the caller to select one and "
            "call with its appointment_id and confirm_cancellation=false. "
            "After reading the selected appointment details, ask for a "
            "separate explicit confirmation. Only then call with the "
            "same appointment_id and confirm_cancellation=true. "
            "Never skip any confirmation stage."
        ),
        properties={
            "phone": {
                "type": "string",
                "description": "Registered patient phone number.",
            },
            "confirm_phone": {
                "type": "boolean",
                "description": (
                    "False to request phone confirmation; true "
                    "only after explicit caller confirmation."
                ),
            },
            "appointment_id": {
                "type": "integer",
                "description": (
                    "Selected appointment ID. Omit when listing "
                    "active appointments."
                ),
            },
            "confirm_cancellation": {
                "type": "boolean",
                "description": (
                    "False while listing or selecting. True only "
                    "after a separate explicit confirmation to cancel "
                    "the selected appointment."
                ),
            },
        },
        required=[
            "phone",
            "confirm_phone",
        ],
        handler=cancellation_handler,
    )

    tools = ToolsSchema(
        standard_tools=[
            availability_schema,
            booking_schema,
            cancellation_schema,
        ]
    )

    # =====================================================
    # 8. GEMINI SYSTEM INSTRUCTIONS
    # =====================================================

    system_instruction = """
You are CareVoice, a polite AI doctor appointment assistant.

YOUR RESPONSIBILITIES:
1. Check doctor availability.
2. Book appointments.
3. Cancel appointments safely.

AVAILABILITY:
- Ask which doctor or specialty the caller needs.
- Always call check_doctor_availability.
- Only offer slots returned by the tool.
- Never invent doctors, dates, times or slot IDs.

BOOKING:
- Collect patient's full name and phone number.
- Read the complete phone number back digit by digit.
- First call book_appointment with confirm_phone=false.
- Ask whether the number is correct.
- Wait for the caller's next spoken response.
- Only after explicit confirmation, call again with confirm_phone=true.
- Never claim booking success unless the tool returns success=true.

CANCELLATION - STRICT MULTI-STEP PROCESS:

STEP 1: PHONE CONFIRMATION
- Collect the registered phone number.
- First call cancel_appointment with confirm_phone=false.
- Read the phone number back digit by digit.
- Ask the caller to confirm it.
- Wait for a new spoken response.
- Only after explicit confirmation, call with confirm_phone=true.

STEP 2: APPOINTMENT LOOKUP
- The tool will return active appointments.
- Do not claim anything has been cancelled.
- If no appointments are found, tell the caller.
- If one appointment is found, read its details.
- If multiple appointments are found, read their IDs, doctors,
  dates and times and ask which one the caller wants to cancel.

STEP 3: APPOINTMENT SELECTION
- For multiple appointments, wait for the caller to select one.
- Call the cancellation tool with the selected appointment_id
  and confirm_cancellation=false.
- Never choose an appointment on the caller's behalf.

STEP 4: FINAL CONFIRMATION
- Read the selected appointment's doctor, date and time back.
- Ask: "Do you want me to cancel this appointment?"
- Wait for a separate new spoken response.
- If the caller says no, stop the cancellation.
- Only after an explicit yes, call with the same appointment_id
  and confirm_cancellation=true.

STEP 5: RESULT
- Never claim cancellation success before the tool returns
  success=true.
- Never cancel more than the selected appointment.
- Never skip phone confirmation or final cancellation confirmation.

GENERAL:
- Speak politely and naturally.
- Keep responses short and clear.
- Do not provide medical diagnoses.
- For medical emergencies, advise contacting local
  emergency services immediately.
"""

    # =====================================================
    # 9. CREATE GEMINI SERVICE
    # =====================================================

    llm = GoogleLLMService(
        api_key=os.environ["GOOGLE_API_KEY"],
        settings=GoogleLLMService.Settings(
            model=os.getenv(
                "GEMINI_MODEL",
                "gemini-3.5-flash-lite",
            ),
        ),
        system_instruction=system_instruction,
    )

    # =====================================================
    # 10. CREATE ELEVENLABS STT
    # =====================================================

    stt = ElevenLabsRealtimeSTTService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
    )

    # =====================================================
    # 11. CREATE ELEVENLABS TTS
    # =====================================================

    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        voice_id=os.environ["ELEVENLABS_VOICE_ID"],
    )

    # =====================================================
    # 12. CREATE SILERO VAD
    # =====================================================

    vad = build_vad_processor()

    # =====================================================
    # 13. CREATE LLM CONTEXT
    # =====================================================

    context = LLMContext(
        messages=[],
        tools=tools,
    )

    # Number-only utterances are often classified as incomplete by the default
    # Smart Turn analyzer. Use VAD silence plus finalized STT text to end the
    # turn, so a spoken phone number reaches Gemini without waiting indefinitely.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=0.6,
                        wait_for_transcript=True,
                    )
                ]
            )
        ),
    )

    # =====================================================
    # 14. BUILD PIPELINE
    # =====================================================

    class TranscriptRelay(FrameProcessor):
        """Send finalized user and assistant text over SmallWebRTC's data channel."""

        def __init__(self, role):
            super().__init__()
            self.role = role
            self.response_text = []

        async def send_transcript(self, text):
            try:
                await output_transport.send_message(
                    OutputTransportMessageFrame({
                        "type": "transcript",
                        "role": self.role,
                        "text": text,
                    })
                )
                logger.info(
                    "Relayed %s transcript text (%d characters)",
                    self.role,
                    len(text),
                )
            except Exception:
                # A transcript transport issue must not stop voice audio or tools.
                logger.exception("Could not relay %s transcript", self.role)

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)

            if direction == FrameDirection.DOWNSTREAM:
                if self.role == "user" and isinstance(frame, TranscriptionFrame):
                    if frame.finalized and frame.text.strip():
                        await self.send_transcript(frame.text.strip())

                elif self.role == "assistant":
                    if isinstance(frame, LLMFullResponseStartFrame):
                        self.response_text = []
                    elif isinstance(frame, LLMTextFrame):
                        self.response_text.append(frame.text)
                    elif isinstance(frame, LLMFullResponseEndFrame):
                        response = "".join(self.response_text).strip()
                        self.response_text = []
                        if response:
                            await self.send_transcript(response)

            await self.push_frame(frame, direction)

    user_transcript_relay = TranscriptRelay("user")
    assistant_transcript_relay = TranscriptRelay("assistant")

    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            user_transcript_relay,
            context_aggregator.user(),
            llm,
            assistant_transcript_relay,
            tts,
            output_transport,
            context_aggregator.assistant(),
        ]
    )

    logger.info("CareVoice Pipecat pipeline created.")
    logger.info("Appointment tools attached to Gemini context.")

    return PipelineTask(pipeline)


# =========================================================
# 15. WORKER INFORMATION
# =========================================================

if __name__ == "__main__":

    print("======================================")
    print("CareVoice Pipecat Worker")
    print("======================================")

    print("Silero VAD: Configured")
    print("ElevenLabs STT: Configured")
    print("Gemini LLM: Configured")
    print("ElevenLabs TTS: Configured")

    print("Appointment Tools:")
    print("1. check_doctor_availability")
    print("2. book_appointment")
    print("3. cancel_appointment")

    print("======================================")
    print("A compatible audio transport is still required.")
