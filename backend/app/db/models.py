from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String)
    garmin_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    garmin_token_dir: Mapped[str | None] = mapped_column(String)
    latitude: Mapped[float | None] = mapped_column(Numeric)
    longitude: Mapped[float | None] = mapped_column(Numeric)
    location_name: Mapped[str | None] = mapped_column(String)
    timezone: Mapped[str | None] = mapped_column(String)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Normalized snapshot of the athlete's Garmin profile — weight, max HR, HR
    # zone floors, VO2max, race predictions, fitness age, lactate threshold.
    # See app/garmin/profile.py for the shape.
    garmin_profile: Mapped[dict | None] = mapped_column(JSON)
    profile_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_progress_eval_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    activities: Mapped[list["Activity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    daily_metrics: Mapped[list["DailyMetric"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    goals: Mapped[list["Goal"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    training_plans: Mapped[list["TrainingPlan"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    memories: Mapped[list["Memory"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("user_id", "garmin_activity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    garmin_activity_id: Mapped[str] = mapped_column(String, index=True)
    activity_type: Mapped[str | None] = mapped_column(String)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    distance_m: Mapped[float | None] = mapped_column(Numeric)
    duration_s: Mapped[float | None] = mapped_column(Numeric)
    avg_pace_s_per_km: Mapped[float | None] = mapped_column(Numeric)
    avg_hr: Mapped[int | None] = mapped_column()
    max_hr: Mapped[int | None] = mapped_column()
    cadence: Mapped[float | None] = mapped_column(Numeric)
    latitude: Mapped[float | None] = mapped_column(Numeric)
    longitude: Mapped[float | None] = mapped_column(Numeric)
    location_name: Mapped[str | None] = mapped_column(String)
    location_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    user: Mapped["User"] = relationship(back_populates="activities")


class DailyMetric(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("user_id", "metric_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    sleep_score: Mapped[int | None] = mapped_column()
    sleep_duration_s: Mapped[float | None] = mapped_column(Numeric)
    hrv: Mapped[float | None] = mapped_column(Numeric)
    resting_hr: Mapped[int | None] = mapped_column()
    stress_avg: Mapped[int | None] = mapped_column()
    body_battery: Mapped[int | None] = mapped_column()
    vo2max: Mapped[float | None] = mapped_column(Numeric)
    training_readiness: Mapped[int | None] = mapped_column()
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    user: Mapped["User"] = relationship(back_populates="daily_metrics")


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    target_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    user: Mapped["User"] = relationship(back_populates="goals")


class TrainingPlan(Base):
    __tablename__ = "training_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"))
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    user: Mapped["User"] = relationship(back_populates="training_plans")
    workouts: Mapped[list["PlanWorkout"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class PlanWorkout(Base):
    __tablename__ = "plan_workouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("training_plans.id"), index=True)
    scheduled_date: Mapped[date] = mapped_column(Date)
    workout_type: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    target_distance_m: Mapped[float | None] = mapped_column(Numeric)
    target_pace_s_per_km: Mapped[float | None] = mapped_column(Numeric)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    garmin_workout_id: Mapped[str | None] = mapped_column(String)
    garmin_scheduled_id: Mapped[str | None] = mapped_column(String)

    plan: Mapped["TrainingPlan"] = relationship(back_populates="workouts")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    user: Mapped["User"] = relationship(back_populates="chat_messages")


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    user: Mapped["User"] = relationship(back_populates="memories")
