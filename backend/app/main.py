"""FastAPI application for endurance race timing review.

Hard rules enforced here:

* Device reads are append-only and idempotent; no endpoint edits or deletes
  a raw read.
* Adjudications are append-only history.  Correcting a decision inserts a
  new row; the latest decision wins in replay.
* There is no endpoint to overwrite a result time.  Results exist only as
  snapshots *generated* by replay, and publication is refused while any
  issue is still unresolved.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import engine as E
from .db import get_session, init_db
from .models import (
    Adjudication,
    Category,
    ChipAssignment,
    ChipRead,
    Competitor,
    Course,
    CourseNode,
    Event,
    ReplayRun,
    ResultSnapshot,
    Wave,
)
from .repo import run_replay
from .schemas import (
    AdjudicationIn,
    CompetitorIn,
    DeviceBatchIn,
    EventIn,
)

app = FastAPI(title="耐力赛事计时复核", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------
def _create_event(session: Session, data: EventIn) -> Event:
    event = Event(name=data.name)
    session.add(event)
    session.flush()

    for w in data.waves:
        session.add(Wave(
            event_id=event.id, name=w.name, gun_time=w.gun_time,
        ))
    for c in data.categories:
        session.add(Category(
            event_id=event.id, name=c.name, laps_required=c.laps_required,
            mixed=c.mixed, rank_by=c.rank_by,
        ))
    for c in data.courses:
        kinds = [n.kind for n in c.nodes]
        if kinds.count("start") != 1 or kinds.count("finish") != 1:
            raise HTTPException(
                422, f"路线 {c.name} 必须恰好包含一个 start 和一个 finish 节点"
            )
        course = Course(event_id=event.id, name=c.name)
        session.add(course)
        session.flush()
        for n in c.nodes:
            session.add(CourseNode(
                course_id=course.id, order_index=n.order_index, code=n.code,
                name=n.name, kind=n.kind, min_split_s=n.min_split_s,
                max_split_s=n.max_split_s,
            ))
    session.flush()
    return event


def _seed_competitor(session: Session, event_id: int, c: CompetitorIn,
                     event: Event) -> None:
    wave = session.get(Wave, c.wave_id)
    course = session.get(Course, c.course_id)
    cat = session.get(Category, c.category_id)
    if not wave or wave.event_id != event_id:
        raise HTTPException(422, f"wave_id {c.wave_id} 不存在")
    if not course or course.event_id != event_id:
        raise HTTPException(422, f"course_id {c.course_id} 不存在")
    if not cat or cat.event_id != event_id:
        raise HTTPException(422, f"category_id {c.category_id} 不存在")
    row = Competitor(
        event_id=event_id, bib=c.bib, name=c.name,
        category_id=c.category_id, wave_id=c.wave_id,
        course_id=c.course_id, initial_chip=c.initial_chip,
        class_label=c.class_label,
    )
    session.add(row)
    session.flush()
    # Initial chip valid from the wave gun time onward (until a recorded
    # chip swap closes the interval).
    session.add(ChipAssignment(
        competitor_id=row.id, chip=c.initial_chip,
        valid_from=wave.gun_time, valid_to=None, source="seed",
        note="报名登记芯片",
    ))


@app.post("/api/events", status_code=201)
def create_event(data: EventIn, session: Session = Depends(get_session)):
    event = _create_event(session, data)
    for c in data.competitors:
        _seed_competitor(session, event.id, c, event)
    session.commit()
    return {"event_id": event.id}


@app.post("/api/events/{event_id}/competitors", status_code=201)
def add_competitors(event_id: int, items: list[CompetitorIn],
                    session: Session = Depends(get_session)):
    """Bulk registration — lets callers resolve wave/category/course ids
    after the event skeleton was created."""
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "赛事不存在")
    ids = []
    for c in items:
        row = Competitor(
            event_id=event_id, bib=c.bib, name=c.name,
            category_id=c.category_id, wave_id=c.wave_id,
            course_id=c.course_id, initial_chip=c.initial_chip,
            class_label=c.class_label,
        )
        # Validate references through the shared seeding helper's checks.
        wave = session.get(Wave, c.wave_id)
        course = session.get(Course, c.course_id)
        cat = session.get(Category, c.category_id)
        if not wave or wave.event_id != event_id:
            raise HTTPException(422, f"wave_id {c.wave_id} 不存在")
        if not course or course.event_id != event_id:
            raise HTTPException(422, f"course_id {c.course_id} 不存在")
        if not cat or cat.event_id != event_id:
            raise HTTPException(422, f"category_id {c.category_id} 不存在")
        session.add(row)
        session.flush()
        session.add(ChipAssignment(
            competitor_id=row.id, chip=c.initial_chip,
            valid_from=wave.gun_time, valid_to=None, source="seed",
            note="报名登记芯片",
        ))
        ids.append(row.id)
    session.commit()
    return {"competitor_ids": ids}


@app.get("/api/events")
def list_events(session: Session = Depends(get_session)):
    rows = session.scalars(select(Event).order_by(Event.id)).all()
    return [{"id": e.id, "name": e.name,
             "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in rows]


@app.get("/api/events/{event_id}")
def get_event(event_id: int, session: Session = Depends(get_session)):
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "赛事不存在")
    return {
        "id": event.id,
        "name": event.name,
        "waves": [
            {"id": w.id, "name": w.name, "gun_time": w.gun_time.isoformat()}
            for w in event.waves
        ],
        "categories": [
            {"id": c.id, "name": c.name, "laps_required": c.laps_required,
             "mixed": c.mixed, "rank_by": c.rank_by}
            for c in event.categories
        ],
        "courses": [
            {
                "id": c.id, "name": c.name,
                "nodes": [
                    {"order_index": n.order_index, "code": n.code,
                     "name": n.name, "kind": n.kind,
                     "min_split_s": n.min_split_s,
                     "max_split_s": n.max_split_s}
                    for n in sorted(c.nodes, key=lambda x: x.order_index)
                ],
            }
            for c in event.courses
        ],
        "competitors": [
            {"id": p.id, "bib": p.bib, "name": p.name,
             "category_id": p.category_id, "wave_id": p.wave_id,
             "course_id": p.course_id, "initial_chip": p.initial_chip,
             "class_label": p.class_label}
            for p in session.scalars(
                select(Competitor)
                .where(Competitor.event_id == event_id)
                .order_by(Competitor.bib)
            ).all()
        ],
    }


# ---------------------------------------------------------------------------
# Simulated device feed (append-only)
# ---------------------------------------------------------------------------
@app.post("/api/events/{event_id}/device-reads")
def ingest_reads(event_id: int, batch: DeviceBatchIn,
                 session: Session = Depends(get_session)):
    if not session.get(Event, event_id):
        raise HTTPException(404, "赛事不存在")
    keys = [r.idempotency_key for r in batch.reads]
    existing = set(session.scalars(
        select(ChipRead.idempotency_key).where(
            ChipRead.event_id == event_id,
            ChipRead.idempotency_key.in_(keys),
        )
    ).all()) if keys else set()
    inserted = 0
    for r in batch.reads:
        if r.idempotency_key in existing:
            continue  # simulator replay of the same batch is a no-op
        session.add(ChipRead(
            event_id=event_id, chip=r.chip, node_code=r.node_code,
            device_id=r.device_id, raw_seq=r.raw_seq,
            read_time=r.read_time, idempotency_key=r.idempotency_key,
        ))
        inserted += 1
    session.commit()
    return {"inserted": inserted,
            "duplicates_ignored": len(batch.reads) - inserted}


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
@app.post("/api/events/{event_id}/replay")
def replay(event_id: int, session: Session = Depends(get_session)):
    if not session.get(Event, event_id):
        raise HTTPException(404, "赛事不存在")
    return run_replay(session, event_id)


@app.get("/api/events/{event_id}/replay/latest")
def latest_replay(event_id: int, session: Session = Depends(get_session)):
    last = session.scalars(
        select(ReplayRun).where(ReplayRun.event_id == event_id)
        .order_by(ReplayRun.id.desc()).limit(1)
    ).first()
    if not last:
        return run_replay(session, event_id)
    return {
        "input_hash": last.input_hash,
        "output_hash": last.output_hash,
        "output": last.output,
        "algo_version": last.algo_version,
        "ran_at": last.created_at.isoformat() if last.created_at else None,
    }


# ---------------------------------------------------------------------------
# Adjudication (append-only)
# ---------------------------------------------------------------------------
def _latest_issues_map(session: Session, event_id: int) -> dict:
    packed = run_replay(session, event_id, persist=False)
    return {i["issue_key"]: i for i in packed["output"]["issues"]}


@app.post("/api/events/{event_id}/issues/{issue_key}/adjudications",
          status_code=201)
def decide_issue(event_id: int, issue_key: str, body: AdjudicationIn,
                 session: Session = Depends(get_session)):
    if not session.get(Event, event_id):
        raise HTTPException(404, "赛事不存在")
    if body.issue_key != issue_key:
        raise HTTPException(422, "路径与请求体中的 issue_key 不一致")

    issues = _latest_issues_map(session, event_id)
    issue = issues.get(issue_key)
    if issue is None:
        raise HTTPException(404, "疑点不存在或已被前序裁定消化（见裁定历史）")
    kind = issue["kind"]
    allowed = E.ALLOWED_DECISIONS.get(kind, ())
    if body.decision not in allowed:
        raise HTTPException(
            422, f"该疑点允许的裁定为: {', '.join(allowed)}"
        )

    competitor_id = issue.get("competitor_id")
    payload: dict = {}

    if body.decision == "ATTACH_CHIP":
        competitor_id = body.competitor_id or competitor_id
        if not competitor_id:
            raise HTTPException(422, "挂接芯片必须提供 competitor_id")
        comp = session.get(Competitor, competitor_id)
        if not comp or comp.event_id != event_id:
            raise HTTPException(422, "competitor_id 不属于该赛事")
        chip = issue["detail"].get("chip")
        valid_from = body.valid_from or _first_read_of_chip(
            session, event_id, chip
        )
        if valid_from is None:
            raise HTTPException(422, "无法确定芯片生效时间，需传 valid_from")
        _apply_chip_swap(
            session, competitor_id, chip, valid_from, body.valid_to,
            body.reason,
        )
        payload = {"chip": chip, "valid_from": valid_from.isoformat(),
                   "valid_to": body.valid_to.isoformat()
                   if body.valid_to else None}

    row = Adjudication(
        event_id=event_id, issue_key=issue_key, competitor_id=competitor_id,
        kind=kind, decision=body.decision, reason=body.reason,
        payload=payload, decided_by=body.decided_by or "official",
    )
    session.add(row)
    session.commit()
    return {"adjudication_id": row.id, "issue_key": issue_key}


def _first_read_of_chip(session: Session, event_id: int, chip: str):
    return session.scalars(
        select(ChipRead.read_time)
        .where(ChipRead.event_id == event_id, ChipRead.chip == chip)
        .order_by(ChipRead.read_time, ChipRead.id).limit(1)
    ).first()


def _apply_chip_swap(session: Session, competitor_id: int, chip: str,
                     valid_from: datetime, valid_to, reason: str) -> None:
    """Close the open chip interval and open the new one.

    The adjudication row is the durable evidence; assignment intervals are
    the derived view replay needs to attribute reads to the stable athlete.
    """
    open_rows = session.scalars(
        select(ChipAssignment)
        .where(ChipAssignment.competitor_id == competitor_id,
               ChipAssignment.valid_to.is_(None))
    ).all()
    for a in open_rows:
        a.valid_to = valid_from
    session.add(ChipAssignment(
        competitor_id=competitor_id, chip=chip, valid_from=valid_from,
        valid_to=valid_to, source="adjudication",
        note=f"换芯片衔接：{reason}",
    ))


@app.post("/api/competitors/{competitor_id}/chip-swaps", status_code=201)
def register_chip_swap(competitor_id: int, body: AdjudicationIn,
                       session: Session = Depends(get_session)):
    """Proactive recording of a mid-race chip change (stable identity)."""
    comp = session.get(Competitor, competitor_id)
    if not comp:
        raise HTTPException(404, "选手不存在")
    if not body.issue_key:
        raise HTTPException(422, "需要提供新芯片号作为 issue_key 载荷")
    chip = body.issue_key  # callers send the new chip code here
    valid_from = body.valid_from or _first_read_of_chip(
        session, comp.event_id, chip
    )
    if valid_from is None:
        raise HTTPException(422, "无法确定芯片生效时间，需传 valid_from")
    _apply_chip_swap(
        session, comp.id, chip, valid_from, body.valid_to, body.reason,
    )
    key = f"COMP:{comp.id}:CHIPSWAP:{valid_from.isoformat()}"
    row = Adjudication(
        event_id=comp.event_id, issue_key=key, competitor_id=comp.id,
        kind="CHIP_SWAP", decision="ATTACH_CHIP", reason=body.reason,
        payload={"chip": chip, "valid_from": valid_from.isoformat()},
        decided_by=body.decided_by or "official",
    )
    session.add(row)
    session.commit()
    return {"adjudication_id": row.id, "issue_key": key}


@app.post("/api/competitors/{competitor_id}/official-adjudication",
          status_code=201)
def official_adjudication(competitor_id: int, body: AdjudicationIn,
                          session: Session = Depends(get_session)):
    """Referee decision: disqualify or reinstate — recorded, not a time edit."""
    comp = session.get(Competitor, competitor_id)
    if not comp:
        raise HTTPException(404, "选手不存在")
    if body.decision not in ("DISQUALIFY", "REINSTATE"):
        raise HTTPException(422, "综合裁定仅允许 DISQUALIFY / REINSTATE")
    key = f"COMP:{competitor_id}:OFFICIAL"
    row = Adjudication(
        event_id=comp.event_id, issue_key=key, competitor_id=competitor_id,
        kind=E.OFFICIAL, decision=body.decision, reason=body.reason,
        payload={}, decided_by=body.decided_by or "official",
    )
    session.add(row)
    session.commit()
    return {"adjudication_id": row.id, "issue_key": key}


@app.get("/api/events/{event_id}/adjudications")
def list_adjudications(event_id: int, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Adjudication).where(Adjudication.event_id == event_id)
        .order_by(Adjudication.id)
    ).all()
    return [
        {
            "id": a.id, "issue_key": a.issue_key, "kind": a.kind,
            "kind_title": E.KIND_TITLES.get(a.kind, a.kind),
            "competitor_id": a.competitor_id, "decision": a.decision,
            "decision_label": E.DECISION_LABELS.get(a.decision, a.decision),
            "reason": a.reason, "decided_by": a.decided_by,
            "decided_at": a.decided_at.isoformat()
            if a.decided_at else None,
            "payload": a.payload,
        }
        for a in rows
    ]


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------
@app.post("/api/events/{event_id}/publish")
def publish(event_id: int, body: dict | None = None,
            session: Session = Depends(get_session)):
    if not session.get(Event, event_id):
        raise HTTPException(404, "赛事不存在")
    decided_by = (body or {}).get("decided_by", "official")
    packed = run_replay(session, event_id)
    out = packed["output"]

    open_issues = [i for i in out["issues"] if i["resolution"] is None]
    if open_issues:
        raise HTTPException(
            409,
            {
                "message": "仍有未逐项确认的疑点，不能发布；成绩不会被直接改写",
                "open_issues": [
                    {"issue_key": i["issue_key"], "kind": i["kind"],
                     "title": i["title"],
                     "competitor_id": i["competitor_id"]}
                    for i in open_issues
                ],
            },
        )

    prev_version = session.scalar(
        select(func.coalesce(func.max(ResultSnapshot.version), 0))
        .where(ResultSnapshot.event_id == event_id)
    ) or 0
    version = prev_version + 1

    # Supersede older published snapshots (derived artefacts, not evidence).
    for old in session.scalars(
        select(ResultSnapshot).where(
            ResultSnapshot.event_id == event_id,
            ResultSnapshot.published.is_(True),
        )
    ).all():
        old.published = False

    created = []
    for r in out["results"]:
        if r["status"] not in ("FINISHER", "DQ"):
            continue  # DNF / unreviewed rows are not published as results
        snap = ResultSnapshot(
            event_id=event_id, competitor_id=r["competitor_id"],
            version=version, published=True,
            laps_confirmed=r["confirmed_laps"],
            total_net_s=r["total_net_s"], total_gun_s=r["total_gun_s"],
            status=r["status"], basis=r, input_hash=packed["input_hash"],
            published_by=decided_by,
            published_at=datetime.now(timezone.utc),
        )
        session.add(snap)
        created.append(r["competitor_id"])
    session.commit()
    return {"version": version, "published_competitor_ids": created,
            "input_hash": packed["input_hash"],
            "output_hash": packed["output_hash"]}


@app.get("/api/events/{event_id}/results/published")
def published_results(event_id: int, session: Session = Depends(get_session)):
    snaps = session.scalars(
        select(ResultSnapshot).where(
            ResultSnapshot.event_id == event_id,
            ResultSnapshot.published.is_(True),
        ).order_by(ResultSnapshot.id)
    ).all()
    if not snaps:
        return {"version": None, "entries": []}
    version = snaps[0].version
    return {
        "version": version,
        "input_hash": snaps[0].input_hash,
        "published_at": snaps[0].published_at.isoformat()
        if snaps[0].published_at else None,
        "entries": [
            {
                "competitor_id": s.competitor_id,
                "bib": s.basis.get("bib"), "name": s.basis.get("name"),
                "category_id": s.basis.get("category_id"),
                "category_name": s.basis.get("category_name"),
                "class_label": s.basis.get("class_label"),
                "mixed": s.basis.get("mixed"),
                "laps_confirmed": s.laps_confirmed,
                "total_net_s": s.total_net_s,
                "total_gun_s": s.total_gun_s,
                "gun_time": s.basis.get("gun_time"),
                "net_start_time": s.basis.get("net_start_time"),
                "net_start_basis": s.basis.get("net_start_basis"),
                "status": s.status,
            }
            for s in snaps
        ],
    }
