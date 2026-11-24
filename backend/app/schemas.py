
from pydantic import BaseModel, Field


class BookingRequest(BaseModel):
    slot_id: int
    patient_name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=7, max_length=20)
    confirm_phone: bool = False


class CancelRequest(BaseModel):
    appointment_id: int | None = None
    phone: str = Field(min_length=7, max_length=20)
    confirm_phone: bool = False
    confirm_cancellation: bool = False


class AssistantRequest(BaseModel):
    message: str
    phone: str | None = None
    patient_name: str | None = None
    conversation_id: str | None = None
