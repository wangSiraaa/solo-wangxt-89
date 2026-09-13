"""SQLAlchemy models.

Durability rules implemented at the schema/API level:

* ``chip_reads`` is append-only (the API never UPDATEs or DELETEs a row).
* ``adjudications`` is append-only history; re-deciding an issue inserts a
  new row and the latest confirmed decision wins during replay.
* Published ``result_snapshots`` are generated artefacts.  There is no
  endpoint to edit a final time: corrections are made by deciding issues,
  which forces a fresh replay and re-publication.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# JSONB on PostgreSQL, plain JSON elsewhere (lightweight unit tests).
JSONCol = JSON().with_variant(JSONB(), "postgresql")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    waves: Mapped[list["Wave"]] = relationship(back_populates="event")
    categories: Mapped[list["Category"]] = relationship(back_populates="event")
    courses: Mapped[list["Course"]] = relationship(back_populates="event")


class Wave(Base):
    """A staggered (batch) start.  Gun time is the wave's mass start."""

    __tablename__ = "waves"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(100))
    gun_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    event: Mapped[Event] = relationship(back_populates="waves")


class Category(Base):
    """Race group (组别).  Mixed groups simply contain competitors of
    different underlying classes; ranking is computed inside the category."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(100))
    laps_required: Mapped[int] = mapped_column(Integer)
    mixed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Rank by net chip time (default) or gun time.
    rank_by: Mapped[str] = mapped_column(String(10), default="net")

    event: Mapped[Event] = relationship(back_populates="categories")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(100))

    nodes: Mapped[list["CourseNode"]] = relationship(
        back_populates="course", order_by="CourseNode.order_index"
    )
    event: Mapped[Event] = relationship(back_populates="courses")


class CourseNode(Base):
    """A timing point on one lap.  ``order_index`` runs in the legal
    direction; the finish node closes a lap.

    ``min_split_s`` / ``max_split_s`` bound the *feasible* time from the
    preceding node on a legal lap (None = unbounded).  They encode both
    physical limits and cut-offs.
    """

    __tablename__ = "course_nodes"
    __table_args__ = (
        UniqueConstraint("course_id", "code", name="uq_node_course_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    order_index: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))  # start | control | finish
    min_split_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_split_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    course: Mapped[Course] = relationship(back_populates="nodes")


class Competitor(Base):
    """The stable athlete identity.  Chip swaps never create a new
    competitor — they attach a new chip to this identity."""

    __tablename__ = "competitors"
    __table_args__ = (
        UniqueConstraint("event_id", "bib", name="uq_competitor_event_bib"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    bib: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    wave_id: Mapped[int] = mapped_column(ForeignKey("waves.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    initial_chip: Mapped[str] = mapped_column(String(40))
    # Sub-class label inside a mixed category (e.g. 男子精英 within an open
    # mixed group).  Ranking is still computed within the category first.
    class_label: Mapped[str | None] = mapped_column(String(40), nullable=True)

    category: Mapped[Category] = relationship()
    wave: Mapped[Wave] = relationship()
    course: Mapped[Course] = relationship()
    assignments: Mapped[list["ChipAssignment"]] = relationship(
        back_populates="competitor", order_by="ChipAssignment.valid_from"
    )


class ChipAssignment(Base):
    """A chip valid for a competitor during [valid_from, valid_to).

    Maintained through the adjudication API (chip swaps are recorded
    events, never silent edits), but seeded for the initial chip.
    """

    __tablename__ = "chip_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    chip: Mapped[str] = mapped_column(String(40), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String(20), default="seed")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    competitor: Mapped[Competitor] = relationship(back_populates="assignments")


class ChipRead(Base):
    """Raw reader record — immutable evidence."""

    __tablename__ = "chip_reads"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "device_id", "raw_seq",
            name="uq_chipread_event_device_seq",
        ),
        UniqueConstraint(
            "event_id", "idempotency_key", name="uq_chipread_event_idem"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id"), index=True
    )
    chip: Mapped[str] = mapped_column(String(40), index=True)
    node_code: Mapped[str] = mapped_column(String(40))
    device_id: Mapped[str] = mapped_column(String(60))
    raw_seq: Mapped[int] = mapped_column(Integer)
    read_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(80))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Adjudication(Base):
    """An official human decision.  Append-only: re-deciding an issue
    inserts a new row; replay uses the latest decision per issue_key."""

    __tablename__ = "adjudications"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    issue_key: Mapped[str] = mapped_column(String(80), index=True)
    competitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitors.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONCol, default=dict)
    decided_by: Mapped[str] = mapped_column(String(80))
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReplayRun(Base):
    """Audit record of a deterministic replay: input hash → output hash."""

    __tablename__ = "replay_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str] = mapped_column(String(64))
    algo_version: Mapped[str] = mapped_column(String(20))
    output: Mapped[dict] = mapped_column(JSONCol)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ResultSnapshot(Base):
    """Published result.  Generated from a replay — never hand-edited."""

    __tablename__ = "result_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "competitor_id", "version",
            name="uq_snapshot_competitor_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    laps_confirmed: Mapped[int] = mapped_column(Integer)
    total_net_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_gun_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    basis: Mapped[dict] = mapped_column(JSONCol)
    input_hash: Mapped[str] = mapped_column(String(64))
    published_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
