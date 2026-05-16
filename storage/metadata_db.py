"""
PostgreSQL metadata store via SQLAlchemy.
Stores flight metadata, investigations, hypotheses, anomalies, and reports.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey,
    Integer, JSON, String, Text, create_engine, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from config.settings import settings


class Base(DeclarativeBase):
    pass


class Flight(Base):
    __tablename__ = "flights"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sha256 = Column(String(64), unique=True, nullable=True)   # null until worker computes it
    filename = Column(Text, nullable=False)
    file_size = Column(BigInteger)
    format = Column(String(32))           # ardupilot_bin, px4_ulog, etc.
    autopilot = Column(String(32))        # ardupilot, px4
    vehicle_class = Column(String(32))    # multirotor, fixed_wing
    fw_version = Column(Text)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    flight_start_time = Column(DateTime, nullable=True)
    duration_s = Column(Float, nullable=True)
    status = Column(String(32), default="uploaded")
    # uploaded | pending_upload | parsing | ready | error
    message_types = Column(JSON, default=list)
    missing_critical = Column(JSON, default=list)
    parameter_count = Column(Integer, default=0)
    raw_path = Column(Text, nullable=True)      # local path (local dev only)
    gcs_raw_uri = Column(Text, nullable=True)   # gs://bucket/raw/{flight_id}/{filename}

    investigations = relationship("Investigation", back_populates="flight")
    anomalies = relationship("Anomaly", back_populates="flight")


class Investigation(Base):
    __tablename__ = "investigations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id = Column(UUID(as_uuid=True), ForeignKey("flights.id"), nullable=False)
    query = Column(Text, nullable=True)
    status = Column(String(32), default="queued")
    # queued | running | complete | error
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    root_cause = Column(Text, nullable=True)
    contributing_factors = Column(JSON, default=list)
    recommendations = Column(JSON, default=list)
    confidence = Column(String(16), nullable=True)  # HIGH / MEDIUM / LOW label
    report_path = Column(Text, nullable=True)
    iteration_count = Column(Integer, default=0)
    agent_findings = Column(JSON, default=dict)
    open_questions = Column(JSON, default=list)

    flight = relationship("Flight", back_populates="investigations")
    hypotheses = relationship("Hypothesis", back_populates="investigation")


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id = Column(UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=False)
    agent_name = Column(String(64))
    text = Column(Text)
    confidence = Column(Float, default=0.0)
    status = Column(String(32), default="forming")
    # forming | supported | refuted | confirmed
    evidence = Column(JSON, default=list)
    reasoning = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    investigation = relationship("Investigation", back_populates="hypotheses")


class Anomaly(Base):
    __tablename__ = "anomalies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id = Column(UUID(as_uuid=True), ForeignKey("flights.id"), nullable=False)
    investigation_id = Column(UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=True)
    timestamp_us = Column(BigInteger, nullable=False)
    end_timestamp_us = Column(BigInteger, nullable=True)
    severity = Column(String(16))          # INFO | WARNING | CRITICAL | FATAL
    category = Column(String(32))         # EKF | GPS | POWER | VIBE | MOTOR | FAILSAFE
    rule_name = Column(String(64))
    description = Column(Text)
    raw_values = Column(JSON, default=dict)
    detected_by = Column(String(64))      # agent or rule name
    correlation_hint = Column(Text, nullable=True)

    flight = relationship("Flight", back_populates="anomalies")


class Baseline(Base):
    __tablename__ = "baselines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vehicle_type = Column(String(64))
    flight_id = Column(UUID(as_uuid=True), ForeignKey("flights.id"), nullable=True)
    phase_metrics = Column(JSON, default=dict)
    embedding_path = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    is_healthy = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def create_db_engine(sync: bool = True):
    url = settings.database_url_sync if sync else settings.database_url
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def create_tables():
    engine = create_db_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine=None):
    eng = engine or create_db_engine()
    return sessionmaker(bind=eng, autocommit=False, autoflush=False)


class MetadataDB:
    def __init__(self, engine=None):
        self.engine = engine or create_db_engine()
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

    def get_session(self) -> Session:
        return self.SessionLocal()

    def create_flight(self, **kwargs) -> Flight:
        with self.SessionLocal() as session:
            flight = Flight(**kwargs)
            session.add(flight)
            session.commit()
            session.refresh(flight)
            return flight

    def get_flight(self, flight_id: str) -> Flight | None:
        try:
            fid = uuid.UUID(flight_id)
        except ValueError:
            return None
        with self.SessionLocal() as session:
            return session.get(Flight, fid)

    def get_flight_by_hash(self, sha256: str) -> Flight | None:
        with self.SessionLocal() as session:
            return (
                session.query(Flight)
                .filter(Flight.sha256 == sha256)
                .first()
            )

    def update_flight_status(self, flight_id: str, status: str, **extra):
        with self.SessionLocal() as session:
            flight = session.get(Flight, uuid.UUID(flight_id))
            if flight:
                flight.status = status
                for k, v in extra.items():
                    setattr(flight, k, v)
                session.commit()

    def create_investigation(self, flight_id: str, query: str) -> Investigation:
        with self.SessionLocal() as session:
            inv = Investigation(
                flight_id=uuid.UUID(flight_id),
                query=query,
                started_at=datetime.utcnow(),
            )
            session.add(inv)
            session.commit()
            session.refresh(inv)
            return inv

    def update_investigation(self, inv_id: str, **kwargs):
        with self.SessionLocal() as session:
            inv = session.get(Investigation, uuid.UUID(inv_id))
            if inv:
                for k, v in kwargs.items():
                    setattr(inv, k, v)
                session.commit()

    def get_investigation(self, inv_id: str) -> Investigation | None:
        try:
            iid = uuid.UUID(inv_id)
        except ValueError:
            return None
        with self.SessionLocal() as session:
            return session.get(Investigation, iid)

    def save_anomalies(self, flight_id: str, anomalies: list[dict]):
        with self.SessionLocal() as session:
            objs = [
                Anomaly(
                    flight_id=uuid.UUID(flight_id),
                    **{k: v for k, v in a.items() if hasattr(Anomaly, k)},
                )
                for a in anomalies
            ]
            session.add_all(objs)
            session.commit()

    def get_anomalies(
        self,
        flight_id: str,
        severity: list[str] | None = None,
        category: str | None = None,
    ) -> list[Anomaly]:
        with self.SessionLocal() as session:
            q = session.query(Anomaly).filter(
                Anomaly.flight_id == uuid.UUID(flight_id)
            )
            if severity:
                q = q.filter(Anomaly.severity.in_(severity))
            if category:
                q = q.filter(Anomaly.category == category)
            return q.order_by(Anomaly.timestamp_us).all()

    def list_flights(self, limit: int = 50, offset: int = 0) -> list[Flight]:
        with self.SessionLocal() as session:
            return (
                session.query(Flight)
                .order_by(Flight.uploaded_at.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )

    def delete_flight(self, flight_id: str) -> None:
        """Delete a flight and all associated records (for test teardown)."""
        fid = uuid.UUID(flight_id)
        with self.SessionLocal() as session:
            inv_ids = [
                r[0] for r in session.query(Investigation.id)
                .filter(Investigation.flight_id == fid).all()
            ]
            if inv_ids:
                session.query(Hypothesis).filter(
                    Hypothesis.investigation_id.in_(inv_ids)
                ).delete(synchronize_session=False)
                session.query(Investigation).filter(
                    Investigation.flight_id == fid
                ).delete(synchronize_session=False)
            session.query(Anomaly).filter(
                Anomaly.flight_id == fid
            ).delete(synchronize_session=False)
            session.query(Flight).filter(Flight.id == fid).delete(synchronize_session=False)
            session.commit()
