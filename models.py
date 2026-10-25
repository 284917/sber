from sqlalchemy import Column, Integer, String, Numeric, ForeignKey, DateTime, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False)  # borrower, creditor, admin, operator
    created_at = Column(DateTime, server_default=func.now())


class Application(Base):
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    funding_amount = Column(Numeric(15, 2), nullable=False)
    currency_code = Column(String(3), nullable=False)
    target_jurisdiction = Column(String(2), nullable=False)
    purpose = Column(String(255), nullable=False)
    status = Column(String(20), default="published")  # published, in_work, agreed
    created_at = Column(DateTime, server_default=func.now())

    # ДОБАВЛЕНО: для подтверждения оператором
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)


class Offer(Base):
    __tablename__ = "offers"
    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("applications.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    funding_amount = Column(Numeric(15, 2), nullable=False)
    annual_interest_rate = Column(Numeric(5, 2), nullable=False)
    platform_commission = Column(Numeric(5, 2), default=0.8)
    lender_commission = Column(Numeric(5, 2), nullable=False)
    total_commission = Column(Numeric(5, 2), nullable=False)

    # ИЗМЕНЕНО: статусы pending, shown, hidden, accepted
    status = Column(String(20), default="pending")

    # ДОБАВЛЕНО: для оператора
    hide_reason = Column(String(255), nullable=True)
    operator_decision_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    # Связи для удобного доступа
    sender = relationship("User", foreign_keys=[user_id])
    application = relationship("Application")


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    doc_type = Column(String(50))
    file_path = Column(String(255))
    status = Column(String(20), default="uploaded")
    created_at = Column(DateTime, server_default=func.now())
    user = relationship("User")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("applications.id"))
    sender_id = Column(Integer, ForeignKey("users.id"))
    text = Column(String(1000))
    file_path = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    sender = relationship("User", foreign_keys=[sender_id])
    application = relationship("Application")