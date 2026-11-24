
from sqlalchemy import String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    specialty: Mapped[str] = mapped_column(String(100), nullable=False)
    experience: Mapped[str] = mapped_column(String(40), default="8 years")
    fee: Mapped[int] = mapped_column(Integer, default=500)

    slots: Mapped[list["Slot"]] = relationship(
        back_populates="doctor",
        cascade="all, delete-orphan"
    )


class Slot(Base):
    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctors.id"),
        nullable=False
    )
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    time: Mapped[str] = mapped_column(String(20), nullable=False)
    is_booked: Mapped[int] = mapped_column(Integer, default=0)

    doctor: Mapped[Doctor] = relationship(back_populates="slots")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctors.id"),
        nullable=False
    )
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("slots.id"),
        nullable=False
    )
    patient_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="booked")

    doctor: Mapped[Doctor] = relationship()
    slot: Mapped[Slot] = relationship()
