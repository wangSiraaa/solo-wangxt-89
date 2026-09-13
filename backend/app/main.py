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
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import engine as E
from .calibration import device_signatures
from .db import get_session, init_db
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
    ResultSnapshot,
    Wave,
)
from .repo import run_replay
from .schemas import (
    AdjudicationIn,
    ClockSyncBatchIn,
    CompetitorIn,
    DeviceBatchIn,
    EventIn,
)

app = FastAPI(title="耐力赛事计时复核", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    _lightweight_migrate()


def _lightweight_migrate() -> None:
    """Add columns introduced after the first release (idempotent)."""
    from .db import engine as _engine
    inspector = inspect(_engine)
    if "chip_reads" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("chip_reads")}
    with _engine.begin() as conn:
        if "received_at" not in cols:
            conn.execute(text(
                "ALTER TABLE chip_reads ADD COLUMN received_at "
                "TIMESTAMP WITH TIME ZONE"))
            # Backfill arrival time for previously stored evidence.
            conn.execute(text(
                "UPDATE chip_reads SET received_at = ingested_at "
                "WHERE received_at IS NULL"))
    acols = {c["name"] for c in inspect(_engine).get_columns("adjudications")} \
        if "adjudications" in inspector.get_table_names() else set()
    with _engine.begin() as conn:
        if "cal_context" not in acols and "adjudications" \
                in inspector.get_table_names():
            conn.execute(text(
                "ALTER TABLE adjudications ADD COLUMN cal_context JSONB "
                "DEFAULT '{}'::jsonb"))


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
    arrival = datetime.now(timezone.utc)
    for r in batch.reads:
        if r.idempotency_key in existing:
            continue  # simulator replay of the same batch is a no-op
        session.add(ChipRead(
            event_id=event_id, chip=r.chip, node_code=r.node_code,
            device_id=r.device_id, raw_seq=r.raw_seq,
            read_time=r.read_time, idempotency_key=r.idempotency_key,
            received_at=r.received_at or arrival,
        ))
        inserted += 1
    session.commit()
    return {"inserted": inserted,
            "duplicates_ignored": len(batch.reads) - inserted}


# ---------------------------------------------------------------------------
# Trusted device clock references (append-only calibration evidence)
# ---------------------------------------------------------------------------
@app.post("/api/events/{event_id}/clock-syncs", status_code=201)
def add_clock_syncs(event_id: int, batch: ClockSyncBatchIn,
                    session: Session = Depends(get_session)):
    if not session.get(Event, event_id):
        raise HTTPException(404, "赛事不存在")
    arrival = datetime.now(timezone.utc)
    inserted, duplicates = 0, 0
    for s in batch.syncs:
        if session.scalar(
            select(func.count(ClockSync.id)).where(
                ClockSync.event_id == event_id,
                ClockSync.device_id == s.device_id,
                ClockSync.idempotency_key == s.idempotency_key,
            )
        ):
            duplicates += 1
            continue
        session.add(ClockSync(
            event_id=event_id, device_id=s.device_id,
            device_time=s.device_time, true_time=s.true_time,
            reference_epsilon_s=s.reference_epsilon_s,
            received_at=s.received_at or arrival, source=s.source,
            note=s.note, idempotency_key=s.idempotency_key,
            recorded_by=s.recorded_by,
        ))
        inserted += 1
    session.commit()
    # New sync evidence can change mapped times; run replay so callers see
    # which prior decisions now need re-verification.
    packed = run_replay(session, event_id, persist=False)
    stale = [i["issue_key"] for i in packed["output"]["issues"]
             if i["context_changed"]]
    return {"inserted": inserted, "duplicates_ignored": duplicates,
            "context_changed_issues": stale,
            "clock_hash": packed["component_hashes"]["clock"]}


@app.get("/api/events/{event_id}/clock-syncs")
def list_clock_syncs(event_id: int, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(ClockSync).where(ClockSync.event_id == event_id)
        .order_by(ClockSync.device_id, ClockSync.id)
    ).all()
    return [
        {
            "id": s.id, "device_id": s.device_id,
            "device_time": s.device_time.isoformat(),
            "true_time": s.true_time.isoformat(),
            "reference_epsilon_s": s.reference_epsilon_s,
            "received_at": s.received_at.isoformat() if s.received_at else None,
            "source": s.source, "note": s.note,
            "recorded_by": s.recorded_by,
        }
        for s in rows
    ]


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
    out = last.output
    hashes = out.pop("component_hashes", None) if isinstance(out, dict) else None
    return {
        "input_hash": last.input_hash,
        "output_hash": last.output_hash,
        "component_hashes": hashes,
        "output": out,
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

    # Capture the calibration evidence this human decision relied on.
    # If that device is re-synced afterwards, replay flags the decision as
    # context_changed instead of silently overriding or discarding it.
    cal_context = issue.get("cal_signatures", {}) or {}

    row = Adjudication(
        event_id=event_id, issue_key=issue_key, competitor_id=competitor_id,
        kind=kind, decision=body.decision, reason=body.reason,
        payload=payload, cal_context=cal_context,
        decided_by=body.decided_by or "official",
    )
    session.add(row)
    session.commit()
    return {"adjudication_id": row.id, "issue_key": issue_key}


def _first_read_of_chip(session: Session, event_id: int, chip: str):
    """Earliest *true* (calibrated) time observed for a chip."""
    from .repo import build_bundle
    bundle = build_bundle(session, event_id)
    matches = sorted(
        (r for r in bundle.reads if r.chip == chip),
        key=lambda r: r.id,
    )
    if not matches:
        return None
    from .calibration import build_device_maps
    estimates, _ = build_device_maps(
        bundle.syncs, bundle.reads, drift_bound=bundle.drift_bound)
    return min(estimates[r.id].point for r in matches)


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
            "cal_context": a.cal_context or {},
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
    stale = [i for i in out["issues"] if i.get("context_changed")]
    if open_issues or stale:
        raise HTTPException(
            409,
            {
                "message": (
                    "仍有未逐项确认的疑点"
                    + ("；且设备校时变更后部分旧裁定需重新核验（旧裁定保留不覆盖）"
                       if stale else "")
                    + "，不能发布；成绩不会被直接改写"
                ),
                "open_issues": [
                    {"issue_key": i["issue_key"], "kind": i["kind"],
                     "title": i["title"],
                     "competitor_id": i["competitor_id"]}
                    for i in open_issues
                ],
                "recalibration_pending": [
                    {"issue_key": i["issue_key"], "kind": i["kind"],
                     "title": E.KIND_TITLES[E.RECALIBRATED],
                     "competitor_id": i["competitor_id"],
                     "devices": i.get("devices", [])}
                    for i in stale
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
            "output_hash": packed["output_hash"],
            "component_hashes": packed["component_hashes"]}


@app.get("/api/events/{event_id}/results/versions")
def published_versions(event_id: int, session: Session = Depends(get_session)):
    """All published versions (old boards remain available for comparison)."""
    versions = session.execute(
        select(ResultSnapshot.version,
               func.min(ResultSnapshot.published_at).label("at"),
               func.count(ResultSnapshot.id).label("n"),
               func.max(ResultSnapshot.input_hash).label("ih"))
        .where(ResultSnapshot.event_id == event_id)
        .group_by(ResultSnapshot.version)
        .order_by(ResultSnapshot.version)
    ).all()
    return [
        {"version": v[0],
         "published_at": v[1].isoformat() if v[1] else None,
         "entry_count": v[2], "input_hash": v[3]}
        for v in versions
    ]


@app.get("/api/events/{event_id}/results/published")
def published_results(event_id: int, version: int | None = None,
                      session: Session = Depends(get_session)):
    q = select(ResultSnapshot).where(
        ResultSnapshot.event_id == event_id)
    if version is None:
        q = q.where(ResultSnapshot.published.is_(True))
    else:
        q = q.where(ResultSnapshot.version == version)
    snaps = session.scalars(q.order_by(ResultSnapshot.id)).all()
    if not snaps:
        return {"version": None, "entries": []}
    ver = snaps[0].version
    return {
        "version": ver,
        "input_hash": snaps[0].input_hash,
        "published_at": snaps[0].published_at.isoformat()
        if snaps[0].published_at else None,
        "entries": [_snapshot_entry(s) for s in snaps],
    }


def _snapshot_entry(s: ResultSnapshot) -> dict:
    return {
        "competitor_id": s.competitor_id,
        "bib": s.basis.get("bib"), "name": s.basis.get("name"),
        "category_id": s.basis.get("category_id"),
        "category_name": s.basis.get("category_name"),
        "class_label": s.basis.get("class_label"),
        "mixed": s.basis.get("mixed"),
        "laps_confirmed": s.laps_confirmed,
        "total_net_s": s.total_net_s,
        "total_net_lo": s.basis.get("total_net_lo"),
        "total_net_hi": s.basis.get("total_net_hi"),
        "total_gun_s": s.total_gun_s,
        "total_gun_lo": s.basis.get("total_gun_lo"),
        "total_gun_hi": s.basis.get("total_gun_hi"),
        "finish_uncertain": s.basis.get("finish_uncertain"),
        "gun_time": s.basis.get("gun_time"),
        "net_start_time": s.basis.get("net_start_time"),
        "net_start_lo": s.basis.get("net_start_lo"),
        "net_start_hi": s.basis.get("net_start_hi"),
        "net_start_basis": s.basis.get("net_start_basis"),
        "status": s.status,
    }


@app.get("/api/events/{event_id}/results/compare")
def compare_results(event_id: int, version: int | None = None,
                    session: Session = Depends(get_session)):
    """Side-by-side: an old published version vs the current replay board.

    Reports rank changes, interval overlaps that make ordering undecidable
    (并列待裁定), and which input layer changed (clock/identity/decisions).
    """
    version = version
    if version is None:
        latest_v = session.scalar(
            select(func.max(ResultSnapshot.version)).where(
                ResultSnapshot.event_id == event_id))
        version = latest_v
    old_snaps = session.scalars(
        select(ResultSnapshot).where(
            ResultSnapshot.event_id == event_id,
            ResultSnapshot.version == version)
    ).all() if version else []
    old = {s.competitor_id: s for s in old_snaps}

    packed = run_replay(session, event_id, persist=False)
    boards = packed["output"]["leaderboard"]
    current = {}
    for b in boards:
        for e in b["entries"]:
            current[e["competitor_id"]] = {**e, "category_id": b["category_id"]}

    rows = []
    for cid, new in sorted(current.items(),
                           key=lambda kv: (kv[1]["rank"] or 9999,
                                           kv[1]["bib"])):
        prev = old.get(cid)
        prev_rank = None
        prev_net = None
        if prev:
            prev_net = prev.total_net_s
            # reconstruct old rank inside category from snapshot order
            cat_snaps = [s for s in old_snaps
                         if s.basis.get("category_id") == new["category_id"]
                         and s.status == "FINISHER"]
            ranked = sorted(
                cat_snaps,
                key=lambda s: (s.total_net_s
                               if s.basis.get("rank_by") == "net"
                               else s.total_gun_s))
            for i, s in enumerate(ranked, start=1):
                if s.competitor_id == cid:
                    prev_rank = i
        metric = new["total_net_s"] if new["rank_by"] == "net" \
            else new["total_gun_s"]
        rows.append({
            "competitor_id": cid, "bib": new["bib"], "name": new["name"],
            "status": new["status"],
            "old_rank": prev_rank, "new_rank": new["rank"],
            "rank_delta": (prev_rank - new["rank"])
            if prev_rank and new["rank"] else None,
            "old_total_s": prev_net if new["rank_by"] == "net"
            else (prev.total_gun_s if prev else None),
            "new_total_s": metric,
            "new_total_lo": new["total_net_lo"] if new["rank_by"] == "net"
            else new["total_gun_lo"],
            "new_total_hi": new["total_net_hi"] if new["rank_by"] == "net"
            else new["total_gun_hi"],
            "tied": new["tied"], "tie_label": new["tie_label"],
            "class_rank": new["class_rank"],
            "class_tied": new["class_tied"],
            "class_tie_label": new.get("class_tie_label"),
        })

    return {
        "old_version": version,
        "component_hashes": packed["component_hashes"],
        "input_hash": packed["input_hash"],
        "rows": rows,
    }
