"""Build a deterministic replay Bundle from database state and persist runs."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import engine as E
from .config import settings
from .models import (
    Adjudication,
    Category,
    ChipAssignment,
    ChipRead,
    ClockSync,
    Competitor,
    Course,
    CourseNode,
    Event,
    ReplayRun,
)


def build_bundle(session: Session, event_id: int) -> E.Bundle:
    courses_rows = session.scalars(
        select(Course).where(Course.event_id == event_id)
    ).all()
    courses: list[E.CourseT] = []
    for c in courses_rows:
        nodes: list[E.NodeT] = []
        for n in sorted(c.nodes, key=lambda x: x.order_index):
            nodes.append(E.NodeT(
                code=n.code, name=n.name, kind=n.kind, order=n.order_index,
                min_split_s=n.min_split_s, max_split_s=n.max_split_s,
            ))
        courses.append(E.CourseT(id=c.id, name=c.name, nodes=tuple(nodes)))

    comp_rows = session.scalars(
        select(Competitor).where(Competitor.event_id == event_id)
    ).all()
    competitors: list[E.CompetitorT] = []
    for p in comp_rows:
        assignments = [
            E.AssignmentT(
                chip=a.chip, valid_from=a.valid_from, valid_to=a.valid_to,
                source=a.source, note=a.note,
            )
            for a in p.assignments
        ]
        competitors.append(E.CompetitorT(
            id=p.id, bib=p.bib, name=p.name,
            category_id=p.category_id, category_name=p.category.name,
            laps_required=p.category.laps_required,
            course_id=p.course_id, gun_time=p.wave.gun_time,
            rank_by=p.category.rank_by, mixed=p.category.mixed,
            class_label=p.class_label, assignments=tuple(assignments),
        ))

    reads_rows = session.scalars(
        select(ChipRead).where(ChipRead.event_id == event_id)
    ).all()
    reads = [
        E.ReadT(
            id=r.id, chip=r.chip, node_code=r.node_code,
            device_id=r.device_id, raw_seq=r.raw_seq,
            read_time=r.read_time, received_at=r.received_at,
        )
        for r in reads_rows
    ]

    sync_rows = session.scalars(
        select(ClockSync).where(ClockSync.event_id == event_id)
    ).all()
    syncs = [
        E.SyncT(
            id=s.id, device_id=s.device_id, device_time=s.device_time,
            true_time=s.true_time, epsilon_s=s.reference_epsilon_s,
            received_at=s.received_at, source=s.source,
        )
        for s in sync_rows
    ]

    dec_rows = session.scalars(
        select(Adjudication).where(Adjudication.event_id == event_id)
    ).all()
    decisions = [
        E.DecisionT(
            id=d.id, issue_key=d.issue_key, kind=d.kind,
            decision=d.decision, reason=d.reason, decided_by=d.decided_by,
            competitor_id=d.competitor_id, payload=d.payload or {},
            cal_context=d.cal_context or {},
            decided_at=d.decided_at,
        )
        for d in dec_rows
    ]

    return E.Bundle(
        repeat_window_s=settings.repeat_read_window_s,
        courses=tuple(courses),
        competitors=tuple(competitors),
        reads=tuple(reads),
        decisions=tuple(decisions),
        syncs=tuple(syncs),
    )


def run_replay(session: Session, event_id: int, *, persist: bool = True) -> dict:
    bundle = build_bundle(session, event_id)
    packed = E.replay_with_hash(bundle)
    if persist:
        row = ReplayRun(
            event_id=event_id,
            input_hash=packed["input_hash"],
            output_hash=packed["output_hash"],
            algo_version=packed["output"]["algo_version"],
            output={
                **packed["output"],
                "component_hashes": packed["component_hashes"],
            },
        )
        session.add(row)
        session.commit()
    return packed
