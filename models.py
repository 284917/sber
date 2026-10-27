from sqlalchemy import Column, Integer, String, Numeric, ForeignKey, DateTime, Boolean, LargeBinary
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

    # Данные юрлица (для заёмщика страна всегда RU, регномер — ИНН)
    company_name = Column(String(255), nullable=True)
    country_code = Column(String(2), nullable=True)
    registration_number = Column(String(50), nullable=True)
    # Причина отказа в KYC, видна компании до повторной отправки документов
    kyc_reject_reason = Column(String(500), nullable=True)

    @property
    def display_name(self):
        return self.company_name or self.email


class Application(Base):
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    funding_amount = Column(Numeric(15, 2), nullable=False)
    currency_code = Column(String(3), nullable=False)
    target_jurisdiction = Column(String(2), nullable=False)
    purpose = Column(String(255), nullable=False)
    term_months = Column(Integer, nullable=True)
    transfer_method = Column(String(30), nullable=True)
    # moderation -> published | rejected; published -> in_work -> agreed
    status = Column(String(20), default="moderation")
    moderation_comment = Column(String(500), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    # Подтверждение сделки оператором
    confirmed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)

    borrower = relationship("User", foreign_keys=[user_id])


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
    extra_terms = Column(String(1000), nullable=True)

    # pending -> shown | hidden (оператор); shown -> accepted | rejected (заёмщик)
    status = Column(String(20), default="pending")

    hide_reason = Column(String(255), nullable=True)
    operator_decision_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

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


class ApplicationEvent(Base):
    """История заявки: каждое изменение статуса и ключевое действие участников."""
    __tablename__ = "application_events"
    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("applications.id"), index=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    text = Column(String(500), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    actor = relationship("User")


class StoredFile(Base):
    __tablename__ = "stored_files"
    path = Column(String(255), primary_key=True)
    content = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
