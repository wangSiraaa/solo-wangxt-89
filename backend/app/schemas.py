from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---- master data ----------------------------------------------------------
class WaveIn(BaseModel):
    name: str
    gun_time: datetime


class CategoryIn(BaseModel):
    name: str
    laps_required: int = Field(ge=1)
    mixed: bool = False
    rank_by: str = "net"


class NodeIn(BaseModel):
    order_index: int = Field(ge=0)
    code: str
    name: str
    kind: str = "control"  # start | control | finish
    min_split_s: float | None = None
    max_split_s: float | None = None


class CourseIn(BaseModel):
    name: str
    nodes: list[NodeIn]


class CompetitorIn(BaseModel):
    bib: str
    name: str
    category_id: int
    wave_id: int
    course_id: int
    initial_chip: str
    class_label: str | None = None  # sub-class inside a mixed group


class EventIn(BaseModel):
    name: str
    waves: list[WaveIn]
    categories: list[CategoryIn]
    courses: list[CourseIn]
    competitors: list[CompetitorIn]


# ---- device feed ----------------------------------------------------------
class DeviceReadIn(BaseModel):
    chip: str
    node_code: str
    device_id: str = "SIM-01"
    raw_seq: int = Field(ge=0)
    read_time: datetime
    # Stable key supplied by the simulator; duplicates are ignored safely.
    idempotency_key: str


class DeviceBatchIn(BaseModel):
    event_id: int
    reads: list[DeviceReadIn]


# ---- adjudication ---------------------------------------------------------
class AdjudicationIn(BaseModel):
    issue_key: str
    decision: str
    reason: str = Field(min_length=4)
    decided_by: str = "official"
    # ATTACH_CHIP requires chip + competitor id + validity window.
    competitor_id: int | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
