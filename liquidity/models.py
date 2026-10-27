"""ORM-модели: компании (users), заявки, предложения, документы KYC, чат, история и файлы."""
from sqlalchemy import Column, Integer, String, Numeric, ForeignKey, DateTime, Date, Boolean, LargeBinary, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from liquidity.database import Base


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
    # Главный администратор: только он создаёт и отключает других сотрудников
    is_superadmin = Column(Boolean, nullable=False, default=False, server_default="false")

    documents = relationship("Document", viewonly=True)
    notifications = relationship("Notification", viewonly=True,
                                 order_by="(Notification.created_at.desc(), Notification.id.desc())")

    @property
    def unread_notifications(self):
        return [n for n in self.notifications if n.read_at is None]

    @property
    def display_name(self):
        return self.company_name or self.email

    @property
    def kyc_status(self):
        """approved / pending / rejected / none — по пакету документов компании."""
        statuses = {d.status for d in self.documents}
        if "approved" in statuses:
            return "approved"
        if "pending" in statuses:
            return "pending"
        return "rejected" if self.kyc_reject_reason else "none"


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


class FxRate(Base):
    """Официальный курс ЦБ РФ (рублей за 1 единицу валюты) — кэш, чтобы не ходить к ЦБ на каждый запрос."""
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("rate_date", "currency_code"),)
    id = Column(Integer, primary_key=True)
    rate_date = Column(Date, nullable=False)
    currency_code = Column(String(3), nullable=False)
    rate = Column(Numeric(18, 6), nullable=False)
    fetched_at = Column(DateTime, server_default=func.now())


class LoginAttempt(Base):
    """Счётчик неудачных входов по ключу «email:…» или «ip:…» — защита от перебора пароля."""
    __tablename__ = "login_attempts"
    key = Column(String(300), primary_key=True)
    failures = Column(Integer, nullable=False, default=0)
    window_start = Column(DateTime, nullable=False)
    locked_until = Column(DateTime, nullable=True)


class Notification(Base):
    """Уведомление в кабинете: новое предложение, решение по KYC, сообщение оператора и т.п."""
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    text = Column(String(500), nullable=False)
    link = Column(String(255), nullable=True)
    kind = Column(String(20), nullable=False, default="info")
    created_at = Column(DateTime, server_default=func.now())
    read_at = Column(DateTime, nullable=True)


class SavedSearch(Base):
    """Сохранённый фильтр кредитора: о новых опубликованных заявках под него приходит уведомление."""
    __tablename__ = "saved_searches"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    country = Column(String(2), nullable=True)
    currency = Column(String(3), nullable=True)
    amount_min = Column(Numeric(15, 2), nullable=True)
    amount_max = Column(Numeric(15, 2), nullable=True)
    term_max = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
