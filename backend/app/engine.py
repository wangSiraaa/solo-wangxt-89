"""Deterministic lap reconstruction (v2 — clock-calibration aware).

The module is a *pure* function of its inputs: master data, raw chip reads,
trusted clock-sync observations and recorded human decisions.  No I/O and no
wall clock, so identical input always yields identical candidates, issues
and hashes.

Layering (the two layers never overwrite each other)
----------------------------------------------------
* **Technical clock layer** — ``calibration.py`` maps device-stamped times
  to true-time *intervals* from trusted sync observations.  It changes only
  derived times.  Raw ``read_time`` and server ``received_at`` are kept in
  the output for every record.
* **Human adjudication layer** — officials decide evidence (keep/drop a
  read, credit a missing node, attach a chip, disqualify).  Each decision
  stores the calibration signatures of the devices it relied on.  If a
  device is later re-synced, affected decisions are flagged
  ``context_changed`` and must be re-verified; the old decision is never
  deleted or silently overridden by clock correction.

Uncertain times never become fake millisecond results: a mapped time is
always a [lo, hi] interval, and finishers whose intervals overlap are ranked
as "并列待裁定" rather than ordered by invented precision.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .calibration import build_device_maps
from .timeutil import iso, utc

ALGO_VERSION = "2.0.0"

GUN_TOLERANCE_S = 2.0

# Issue kinds
DUP = "DUPLICATE_READ"
MISSING = "MISSING_NODE"
REVERSE = "REVERSE_ORDER"
REPEAT_FINISH = "REPEAT_FINISH"
IMPLAUSIBLE = "IMPLAUSIBLE_SPLIT"
UNKNOWN_NODE = "UNKNOWN_NODE"
UNKNOWN_CHIP = "UNKNOWN_CHIP"
BEFORE_GUN = "BEFORE_GUN"
OFFICIAL = "OFFICIAL"
RECALIBRATED = "RECALIBRATED"

KIND_TITLES = {
    DUP: "同一感应点短时重复读卡（可能站在感应区 / 换芯片重叠）",
    MISSING: "漏点：经过下一节点但缺少该节点读卡",
    REVERSE: "逆向记录：节点顺序倒退",
    REPEAT_FINISH: "终点重复读卡：新的一圈还是停留在终点？",
    IMPLAUSIBLE: "用时超出可行区间（过快或超过关门时间）",
    UNKNOWN_NODE: "未知路线节点编号",
    UNKNOWN_CHIP: "芯片未登记到任何选手",
    BEFORE_GUN: "起点读卡早于枪声时间",
    OFFICIAL: "裁判长综合裁定",
    RECALIBRATED: "设备校时变更后，原裁定的时间依据需重新核验",
}

ALLOWED_DECISIONS = {
    DUP: ("KEEP_FIRST", "KEEP_LATEST"),
    MISSING: ("CREDIT_NODE", "DISALLOW_LAP"),
    REVERSE: ("ACCEPT_READ", "REJECT_READ"),
    REPEAT_FINISH: ("CREDIT_LAP", "REJECT_READ"),
    IMPLAUSIBLE: ("CONFIRM_READ", "REJECT_READ"),
    UNKNOWN_NODE: ("REJECT_READ",),
    UNKNOWN_CHIP: ("ATTACH_CHIP", "REJECT_CHIP"),
    BEFORE_GUN: ("CONFIRM_READ", "REJECT_READ"),
    OFFICIAL: ("DISQUALIFY", "REINSTATE"),
}

DECISION_LABELS = {
    "KEEP_FIRST": "采用最早读卡",
    "KEEP_LATEST": "采用最晚读卡",
    "CREDIT_NODE": "认可漏点（人工确认经过）",
    "DISALLOW_LAP": "该圈不予计圈",
    "ACCEPT_READ": "接受该读卡",
    "REJECT_READ": "剔除该读卡",
    "CREDIT_LAP": "认可完成一圈",
    "CONFIRM_READ": "确认读卡有效（用时确实如此）",
    "ATTACH_CHIP": "将芯片挂接到选手（换芯片衔接）",
    "REJECT_CHIP": "芯片不属于本场选手",
    "DISQUALIFY": "取消成绩（DSQ）",
    "REINSTATE": "恢复成绩资格",
}

ACCEPTING = {
    "KEEP_FIRST", "KEEP_LATEST", "CREDIT_NODE", "ACCEPT_READ",
    "CREDIT_LAP", "CONFIRM_READ",
}
REJECTING = {"REJECT_READ", "REJECT_CHIP"}


# ---------------------------------------------------------------------------
# Input types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeT:
    code: str
    name: str
    kind: str
    order: int
    min_split_s: float | None = None
    max_split_s: float | None = None


@dataclass(frozen=True)
class CourseT:
    id: int
    name: str
    nodes: tuple[NodeT, ...]


@dataclass(frozen=True)
class AssignmentT:
    chip: str
    valid_from: datetime | None
    valid_to: datetime | None
    source: str = "seed"
    note: str | None = None


@dataclass(frozen=True)
class CompetitorT:
    id: int
    bib: str
    name: str
    category_id: int
    category_name: str
    laps_required: int
    course_id: int
    gun_time: datetime
    rank_by: str = "net"
    mixed: bool = False
    class_label: str | None = None
    assignments: tuple[AssignmentT, ...] = ()


@dataclass(frozen=True)
class ReadT:
    id: int
    chip: str
    node_code: str
    device_id: str
    raw_seq: int
    read_time: datetime
    received_at: datetime | None = None


@dataclass(frozen=True)
class SyncT:
    id: int
    device_id: str
    device_time: datetime
    true_time: datetime
    epsilon_s: float = 0.0
    received_at: datetime | None = None
    source: str = "manual"


@dataclass(frozen=True)
class DecisionT:
    id: int
    issue_key: str
    kind: str
    decision: str
    reason: str
    decided_by: str
    competitor_id: int | None = None
    payload: dict = field(default_factory=dict)
    cal_context: dict = field(default_factory=dict)
    decided_at: datetime | None = None


@dataclass
class Bundle:
    repeat_window_s: float
    courses: tuple[CourseT, ...]
    competitors: tuple[CompetitorT, ...]
    reads: tuple[ReadT, ...]
    decisions: tuple[DecisionT, ...] = ()
    syncs: tuple[SyncT, ...] = ()
    drift_bound: float = 0.001


# ---------------------------------------------------------------------------
# Canonical serialization / layered hashes
# ---------------------------------------------------------------------------
def _canon(value):
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items()}
    return value


def _dump(value) -> str:
    return json.dumps(
        _canon(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def canonical_input(bundle: Bundle) -> dict:
    return {
        "algo_version": ALGO_VERSION,
        "settings": {
            "repeat_window_s": bundle.repeat_window_s,
            "drift_bound": bundle.drift_bound,
        },
        "master": _master_part(bundle),
        "observations": {
            "reads": [
                {
                    "id": r.id, "chip": r.chip, "node_code": r.node_code,
                    "device_id": r.device_id, "raw_seq": r.raw_seq,
                    "read_time": r.read_time, "received_at": r.received_at,
                }
                for r in sorted(bundle.reads, key=lambda x: x.id)
            ],
            "clock_syncs": [
                {
                    "id": s.id, "device_id": s.device_id,
                    "device_time": s.device_time, "true_time": s.true_time,
                    "epsilon_s": s.epsilon_s, "received_at": s.received_at,
                    "source": s.source,
                }
                for s in sorted(bundle.syncs, key=lambda x: x.id)
            ],
        },
        "decisions": [
            {
                "id": d.id, "issue_key": d.issue_key, "kind": d.kind,
                "decision": d.decision, "reason": d.reason,
                "decided_by": d.decided_by, "competitor_id": d.competitor_id,
                "payload": d.payload, "cal_context": d.cal_context,
                "decided_at": d.decided_at,
            }
            for d in sorted(bundle.decisions, key=lambda x: x.id)
        ],
    }


def _master_part(bundle):
    return {
        "courses": [
            {
                "id": c.id, "name": c.name,
                "nodes": [
                    {"code": n.code, "name": n.name, "kind": n.kind,
                     "order": n.order, "min_split_s": n.min_split_s,
                     "max_split_s": n.max_split_s}
                    for n in sorted(c.nodes, key=lambda x: x.order)
                ],
            }
            for c in sorted(bundle.courses, key=lambda x: x.id)
        ],
        "competitors": [
            {
                "id": p.id, "bib": p.bib, "name": p.name,
                "category_id": p.category_id,
                "category_name": p.category_name,
                "laps_required": p.laps_required, "course_id": p.course_id,
                "gun_time": p.gun_time, "rank_by": p.rank_by,
                "mixed": p.mixed, "class_label": p.class_label,
            }
            for p in sorted(bundle.competitors, key=lambda x: x.id)
        ],
    }


def component_hashes(bundle: Bundle) -> dict:
    """Layered fingerprints explaining *why* a conclusion changed:

    master       — course nodes / feasible splits / groups / gun times
    clock        — trusted sync observations (technical clock correction)
    observations — raw device records (device stamps + receive times)
    identity     — chip↔competitor assignment intervals (chip swaps)
    decisions    — append-only human rulings
    """
    canon = canonical_input(bundle)
    assignments = []
    for p in sorted(bundle.competitors, key=lambda x: x.id):
        for a in sorted(p.assignments, key=lambda x: (x.chip,
                            iso(x.valid_from) or "")):
            assignments.append({
                "competitor_id": p.id, "chip": a.chip,
                "valid_from": a.valid_from, "valid_to": a.valid_to,
                "source": a.source, "note": a.note,
            })
    return {
        "master": _hash(canon["master"]),
        "clock": _hash(canon["observations"]["clock_syncs"]),
        "observations": _hash({
            "reads": canon["observations"]["reads"],
            "settings": canon["settings"],
        }),
        "identity": _hash(assignments),
        "decisions": _hash(canon["decisions"]),
    }


def input_hash(bundle: Bundle) -> str:
    return _hash(canonical_input(bundle))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_issue(key, kind, *, competitor_id, read_id=None, detail=None,
                options=None, devices=None, cal_signatures=None):
    return {
        "issue_key": key,
        "kind": kind,
        "title": KIND_TITLES[kind],
        "competitor_id": competitor_id,
        "read_id": read_id,
        "detail": detail or {},
        "options": list(options or ALLOWED_DECISIONS[kind]),
        "resolution": None,
        # devices whose calibration this issue's timing depends on
        "devices": sorted(set(devices or [])),
        "cal_signatures": cal_signatures or {},
        "context_changed": False,
    }


def _attributed(competitors, reads, point_of):
    """Attribute reads to competitors.

    Assignment validity is judged on the *true* (calibrated) point time so
    a chip swapped mid-race bridges correctly even if the device clock is
    drifting.
    """
    by_id = {p.id: [] for p in competitors}
    unknown = []
    for r in sorted(reads, key=lambda x: (point_of(x.id), x.id)):
        t = point_of(r.id)
        owner = None
        for p in competitors:
            for a in p.assignments:
                if a.chip != r.chip:
                    continue
                if a.valid_from is not None and t < utc(a.valid_from):
                    continue
                if a.valid_to is not None and t >= utc(a.valid_to):
                    continue
                owner = p.id
                break
            if owner is not None:
                break
        (by_id[owner] if owner is not None else unknown).append(r)
    return by_id, unknown


def _split_ok(node, seconds):
    if node.min_split_s is not None and seconds < node.min_split_s:
        return False
    if node.max_split_s is not None and seconds > node.max_split_s:
        return False
    return True


def _cal_brief(est):
    return {
        "raw_time": iso(est.raw_time),
        "received_at": iso(est.received_at),
        "true_lo": iso(est.lo),
        "true_point": iso(est.point),
        "true_hi": iso(est.hi),
        "uncertainty_s": round(est.uncertainty_s, 3),
        "basis": est.basis,
        "rate": round(est.rate, 9),
        "offset_s": round(est.offset_s, 3),
        "sync_ids": list(est.sync_ids),
        "device_id": est.device_id,
        "uncertain": est.hi > est.lo + timedelta(microseconds=500_000),
    }


def _tl_entry(r, treatment, est, issue_key=None):
    return {
        "read_id": r.id, "chip": r.chip, "node_code": r.node_code,
        "time": iso(est.point),
        "time_lo": iso(est.lo), "time_hi": iso(est.hi),
        "raw_time": iso(r.read_time), "received_at": iso(r.received_at),
        "device_id": r.device_id, "raw_seq": r.raw_seq,
        "treatment": treatment, "issue_key": issue_key,
        "calibration": _cal_brief(est),
    }


def _cal_changed(cal_context: dict, current_sigs: dict, dev: str) -> bool:
    """True if a device's calibration evidence changed since a decision.

    Both count: an already-referenced device whose sync signature differs,
    and a device that had *no* trusted sync at decision time but has one
    now (its first calibration is itself a context change).
    """
    before = cal_context.get(dev)
    after = current_sigs.get(dev)
    if before is None:
        return after is not None
    return before != after


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def compute_replay(bundle: Bundle) -> dict:
    courses = {c.id: c for c in bundle.courses}
    estimates, sigs = build_device_maps(
        bundle.syncs, bundle.reads, drift_bound=bundle.drift_bound)

    def est_of(rid):
        return estimates[rid]

    def point_of(rid):
        return utc(estimates[rid].point)

    per_comp, unknown_reads = _attributed(
        bundle.competitors, bundle.reads, point_of)

    decisions_map: dict[str, DecisionT] = {}
    for d in sorted(bundle.decisions, key=lambda x: x.id):
        decisions_map[d.issue_key] = d

    all_issues: list[dict] = []
    results: list[dict] = []

    def sigs_of_devices(devs):
        # Explicit None for a device that had no trusted sync at decision
        # time, so adding its first sync afterwards is detectable.
        return {dev: sigs.get(dev) for dev in sorted(set(devs))}

    # --- unknown chips ---------------------------------------------------
    unknown_by_chip: dict[str, list[ReadT]] = {}
    for r in unknown_reads:
        unknown_by_chip.setdefault(r.chip, []).append(r)
    for chip, group in sorted(unknown_by_chip.items()):
        group.sort(key=lambda x: (point_of(x.id), x.id))
        devs = sorted({g.device_id for g in group})
        all_issues.append(_make_issue(
            f"CHIP:{chip}", UNKNOWN_CHIP, competitor_id=None,
            read_id=group[0].id, devices=devs,
            cal_signatures=sigs_of_devices(devs),
            detail={
                "chip": chip,
                "first_seen": iso(point_of(group[0].id)),
                "read_count": len(group),
                "nodes": sorted({g.node_code for g in group}),
            },
        ))

    # --- per competitor --------------------------------------------------
    for p in sorted(bundle.competitors, key=lambda x: x.id):
        course = courses[p.course_id]
        nodes = sorted(course.nodes, key=lambda n: n.order)
        by_code = {n.code: n for n in nodes}
        start_node = next(n for n in nodes if n.kind == "start")
        finish_node = next(n for n in nodes if n.kind == "finish")
        raw = sorted(per_comp[p.id], key=lambda x: (point_of(x.id), x.id))

        timeline: list[dict] = []
        laps: list[dict] = []
        issues: list[dict] = []

        # ---- repeat clustering: same node within window, ACROSS chips
        # (catches both standing on the mat and chip-swap overlaps) ------
        clusters: list[list[ReadT]] = []
        for r in raw:
            if clusters:
                head = clusters[-1][0]
                if (
                    head.node_code == r.node_code
                    and (point_of(r.id) - point_of(head.id)).total_seconds()
                    <= bundle.repeat_window_s
                ):
                    clusters[-1].append(r)
                    continue
            clusters.append([r])

        chosen_ids: dict[int, tuple[str, bool, list[ReadT]]] = {}
        walk_reads: list[ReadT] = []
        for cluster in clusters:
            key = f"R{cluster[0].id}:DUP"
            if len(cluster) > 1:
                devs = sorted({c.device_id for c in cluster})
                issues.append(_make_issue(
                    key, DUP, competitor_id=p.id, read_id=cluster[0].id,
                    devices=devs, cal_signatures=sigs_of_devices(devs),
                    detail={
                        "node_code": cluster[0].node_code,
                        "window_s": bundle.repeat_window_s,
                        "chips": sorted({c.chip for c in cluster}),
                        "reads": [
                            {
                                "read_id": c.id, "time": iso(point_of(c.id)),
                                "raw_time": iso(c.read_time),
                                "device_id": c.device_id, "chip": c.chip,
                                "calibration": _cal_brief(est_of(c.id)),
                            }
                            for c in cluster
                        ],
                    },
                ))
                dec = decisions_map.get(key)
                keep_latest = bool(dec and dec.decision == "KEEP_LATEST")
            else:
                keep_latest = False
            chosen = cluster[-1] if keep_latest else cluster[0]
            chosen_ids[chosen.id] = (key, len(cluster) > 1, cluster)
            walk_reads.append(chosen)

        # ---- forward walk -----------------------------------------------
        lap_no = 1
        expected_order = start_node.order
        held_orders: set[int] = set()
        anchor_point = utc(p.gun_time)
        current = {
            "lap_no": lap_no,
            "segments": [],
            "missing_nodes": [],
            "issue_keys": [],
            "start_point": None,
            "lap_anchor": utc(p.gun_time),
        }

        def add_segment(node, r, feasible, gap, note=None):
            e = est_of(r.id)
            current["segments"].append({
                "node_code": node.code, "node_name": node.name,
                "kind": node.kind,
                "time": iso(e.point), "time_lo": iso(e.lo),
                "time_hi": iso(e.hi), "uncertain": e.hi > e.lo,
                "read_id": r.id, "chip": r.chip, "gap_s": round(gap, 3),
                "feasible": feasible, "note": note,
                "calibration_basis": e.basis,
            })

        for r in walk_reads:
            e = est_of(r.id)
            t = utc(e.point)
            dupkey, is_dup, cluster = chosen_ids[r.id]
            if is_dup:
                for sib in cluster:
                    if sib.id != r.id:
                        timeline.append(
                            _tl_entry(sib, "duplicate_dropped",
                                      est_of(sib.id), dupkey))

            node = by_code.get(r.node_code)
            if node is None:
                key = f"R{r.id}:UNKNOWN_NODE"
                issues.append(_make_issue(
                    key, UNKNOWN_NODE, competitor_id=p.id, read_id=r.id,
                    devices=[r.device_id],
                    cal_signatures=sigs_of_devices([r.device_id]),
                    detail={"node_code": r.node_code}))
                timeline.append(
                    _tl_entry(r, "unknown_node_dropped", e, key))
                continue

            gap = (t - anchor_point).total_seconds()

            if node.order == expected_order:
                block_key = None
                if (
                    node.kind == "start"
                    and current["start_point"] is None
                    and t < utc(p.gun_time) - timedelta(seconds=GUN_TOLERANCE_S)
                ):
                    block_key = f"R{r.id}:BEFORE_GUN"
                    issues.append(_make_issue(
                        block_key, BEFORE_GUN, competitor_id=p.id,
                        read_id=r.id, devices=[r.device_id],
                        cal_signatures=sigs_of_devices([r.device_id]),
                        detail={
                            "gun_time": iso(p.gun_time),
                            "read_time": iso(t),
                            "time_interval": [iso(e.lo), iso(e.hi)],
                            "delta_s": round(
                                (t - utc(p.gun_time)).total_seconds(), 3),
                        }))
                if block_key is None and not _split_ok(node, gap):
                    block_key = f"R{r.id}:IMPLAUSIBLE"
                    issues.append(_make_issue(
                        block_key, IMPLAUSIBLE, competitor_id=p.id,
                        read_id=r.id, devices=[r.device_id],
                        cal_signatures=sigs_of_devices([r.device_id]),
                        detail={
                            "node_code": node.code, "gap_s": round(gap, 3),
                            "min_split_s": node.min_split_s,
                            "max_split_s": node.max_split_s,
                            "time_interval": [iso(e.lo), iso(e.hi)],
                            "calibration_basis": e.basis,
                        }))

                if block_key is not None:
                    dec = decisions_map.get(block_key)
                    if not (dec and dec.decision in ACCEPTING):
                        if dec and dec.decision in REJECTING:
                            held_orders.discard(node.order)
                            mkey = f"R{r.id}:MISS:{node.code}"
                            issues.append(_make_issue(
                                mkey, MISSING, competitor_id=p.id,
                                read_id=r.id, devices=[r.device_id],
                                cal_signatures=sigs_of_devices([r.device_id]),
                                detail={
                                    "node_code": node.code,
                                    "node_name": node.name, "lap_no": lap_no,
                                    "revealed_by_read_id": r.id,
                                    "origin": "rejected_read",
                                }))
                            current["missing_nodes"].append(node.code)
                            current["issue_keys"].append(mkey)
                        else:
                            held_orders.add(node.order)
                        timeline.append(_tl_entry(
                            r, "rejected" if dec else "held_review",
                            e, block_key))
                        continue
                    current["issue_keys"].append(block_key)

                add_segment(
                    node, r, block_key is None,
                    max(0.0, gap) if node.kind == "start" else gap)
                timeline.append(
                    _tl_entry(r, "accepted", e, dupkey if is_dup else None))
                if node.kind == "start" and current["start_point"] is None:
                    current["start_point"] = t
                anchor_point = t
                expected_order += 1

            elif node.order > expected_order:
                missing = [n for n in nodes
                           if expected_order <= n.order < node.order]
                repeat_finish = (
                    node.kind == "finish"
                    and expected_order == start_node.order
                    and lap_no > 1
                )

                if repeat_finish:
                    key = f"R{r.id}:REPEAT_FINISH"
                    issues.append(_make_issue(
                        key, REPEAT_FINISH, competitor_id=p.id, read_id=r.id,
                        devices=[r.device_id],
                        cal_signatures=sigs_of_devices([r.device_id]),
                        detail={
                            "lap_no": lap_no,
                            "missing_nodes": [n.code for n in missing],
                            "gap_from_last_finish_s": round(gap, 3),
                            "time_interval": [iso(e.lo), iso(e.hi)],
                        }))
                    dec = decisions_map.get(key)
                    if not (dec and dec.decision == "CREDIT_LAP"):
                        timeline.append(_tl_entry(
                            r, "rejected" if dec else "held_review",
                            e, key))
                        continue
                    current["issue_keys"].append(key)
                    current["missing_nodes"] = [n.code for n in missing]
                    feasible = True
                    note = "credited_by_repeat_finish_decision"
                else:
                    note = "forward_jump" if missing else None
                    for m in missing:
                        if m.order in held_orders:
                            held_orders.discard(m.order)
                            continue
                        key = f"R{r.id}:MISS:{m.code}"
                        issues.append(_make_issue(
                            key, MISSING, competitor_id=p.id, read_id=r.id,
                            devices=[r.device_id],
                            cal_signatures=sigs_of_devices([r.device_id]),
                            detail={
                                "node_code": m.code, "node_name": m.name,
                                "lap_no": lap_no,
                                "revealed_by_read_id": r.id,
                            }))
                        current["missing_nodes"].append(m.code)
                        current["issue_keys"].append(key)
                    feasible = not missing

                add_segment(node, r, feasible, gap, note)
                timeline.append(_tl_entry(
                    r, "accepted", e,
                    f"R{r.id}:REPEAT_FINISH" if repeat_finish else None))
                anchor_point = t
                expected_order = node.order + 1

            else:
                key = f"R{r.id}:REVERSE"
                issues.append(_make_issue(
                    key, REVERSE, competitor_id=p.id, read_id=r.id,
                    devices=[r.device_id],
                    cal_signatures=sigs_of_devices([r.device_id]),
                    detail={
                        "node_code": node.code,
                        "expected_order": expected_order,
                        "actual_order": node.order, "lap_no": lap_no,
                        "time_interval": [iso(e.lo), iso(e.hi)],
                    }))
                dec = decisions_map.get(key)
                accepted = bool(dec and dec.decision == "ACCEPT_READ")
                if not accepted:
                    timeline.append(_tl_entry(
                        r, "rejected" if dec else "held_review", e, key))
                    continue
                add_segment(node, r, True, gap, "forced_by_official")
                timeline.append(_tl_entry(r, "accepted", e, key))
                anchor_point = t
                expected_order = node.order + 1

            # close lap on accepted finish
            if node.kind == "finish" and expected_order > finish_node.order:
                voided = any(
                    (d := decisions_map.get(k)) is not None
                    and d.decision == "DISALLOW_LAP"
                    for k in current["issue_keys"]
                )
                unresolved = [
                    k for k in current["issue_keys"]
                    if k not in decisions_map
                ]
                lap_status = (
                    "void" if voided
                    else "in_review" if unresolved
                    else "confirmed"
                )
                sp = current["start_point"]
                laps.append({
                    "lap_no": lap_no,
                    "start_time": iso(sp),
                    "start_lo": iso(sp), "start_hi": iso(sp),
                    "start_basis": "start_mat" if sp
                    else "lap_anchor_missing_start_mat",
                    "finish_time": iso(t),
                    "finish_lo": iso(e.lo), "finish_hi": iso(e.hi),
                    "finish_uncertain": e.hi > e.lo
                    + timedelta(microseconds=500_000),
                    "net_s": round(
                        (t - (sp or current["lap_anchor"]))
                        .total_seconds(), 3),
                    "gun_elapsed_s": round(
                        (t - utc(p.gun_time)).total_seconds(), 3),
                    "segments": current["segments"],
                    "missing_nodes": current["missing_nodes"],
                    "issue_keys": current["issue_keys"],
                    "status": lap_status,
                })
                lap_no += 1
                expected_order = start_node.order
                held_orders = set()
                current = {
                    "lap_no": lap_no, "segments": [],
                    "missing_nodes": [], "issue_keys": [],
                    "start_point": None, "lap_anchor": t,
                }

        partial = None
        if current["segments"]:
            partial = {
                "lap_no": lap_no,
                "segments": current["segments"],
                "missing_nodes": current["missing_nodes"],
                "issue_keys": current["issue_keys"],
                "status": "in_progress",
            }

        # ---- resolution + calibration-context staleness ----------------
        for issue in issues:
            d = decisions_map.get(issue["issue_key"])
            if d is not None:
                # technical re-sync invalidates temporal context, but never
                # erases the human decision
                changed = any(_cal_changed(d.cal_context, issue["cal_signatures"], dev)
                              for dev in issue["devices"])
                issue["context_changed"] = changed
                issue["resolution"] = {
                    "decision": d.decision,
                    "decision_label": DECISION_LABELS.get(d.decision, d.decision),
                    "reason": d.reason,
                    "decided_by": d.decided_by,
                    "decided_at": iso(d.decided_at),
                    "payload": d.payload,
                    "cal_context": d.cal_context,
                    "needs_reverification": changed,
                }
        all_issues.extend(issues)

        # ---- totals -----------------------------------------------------
        confirmed_laps = [l for l in laps if l["status"] == "confirmed"]
        open_issue_keys = sorted({
            i["issue_key"] for i in issues if i["resolution"] is None
        })
        stale_issue_keys = sorted({
            i["issue_key"] for i in issues if i["context_changed"]
        })
        official = [
            d for d in bundle.decisions
            if d.kind == OFFICIAL and d.competitor_id == p.id
        ]
        is_dq = bool(official) and official[-1].decision == "DISQUALIFY"

        # net start
        first_lap = laps[0] if laps else None
        if first_lap and first_lap["start_time"]:
            net_start_dt = datetime.fromisoformat(first_lap["start_time"])
            net_start = first_lap["start_time"]
            net_start_lo = net_start_hi = net_start
            net_start_basis = "first_start_mat_read"
        else:
            seg_start = next(
                (s for s in current["segments"] if s["kind"] == "start"),
                None)
            if seg_start:
                net_start_dt = datetime.fromisoformat(seg_start["time"])
                net_start = seg_start["time"]
                net_start_lo = seg_start["time_lo"]
                net_start_hi = seg_start["time_hi"]
                net_start_basis = "first_start_mat_read"
            else:
                net_start_dt = utc(p.gun_time)
                net_start = net_start_lo = net_start_hi = iso(p.gun_time)
                net_start_basis = "gun_fallback_start_mat_missing"

        final_finish = None
        total_net_s = total_gun_s = None
        total_net_lo = total_net_hi = total_gun_lo = total_gun_hi = None
        finish_uncertain = False
        if 0 < p.laps_required <= len(confirmed_laps):
            sl = confirmed_laps[p.laps_required - 1]
            final_finish = sl["finish_time"]
            flo = utc(sl["finish_lo"])
            fhi = utc(sl["finish_hi"])
            total_gun_s = round(
                (utc(final_finish) - utc(p.gun_time)).total_seconds(), 3)
            total_gun_lo = round(
                (flo - utc(p.gun_time)).total_seconds(), 3)
            total_gun_hi = round(
                (fhi - utc(p.gun_time)).total_seconds(), 3)
            nlo = datetime.fromisoformat(net_start_lo)
            nhi = datetime.fromisoformat(net_start_hi)
            total_net_s = round(
                (utc(final_finish) - net_start_dt).total_seconds(), 3)
            total_net_lo = round((flo - utc(nlo)).total_seconds(), 3)
            total_net_hi = round((fhi - utc(nhi)).total_seconds(), 3)
            finish_uncertain = sl["finish_uncertain"] or fhi > flo + \
                timedelta(microseconds=500_000)

        chip_switches = []
        prev_chip = None
        for ev in sorted(timeline, key=lambda x: (x["time"], x["read_id"])):
            if prev_chip is not None and ev["chip"] != prev_chip:
                chip_switches.append({
                    "at": ev["time"], "from_chip": prev_chip,
                    "to_chip": ev["chip"],
                })
            prev_chip = ev["chip"]

        if is_dq:
            status = "DQ"
        elif final_finish is not None and not open_issue_keys \
                and not stale_issue_keys:
            status = "FINISHER"
        elif final_finish is not None:
            status = "FINISHER_PENDING_REVIEW"
        elif open_issue_keys or stale_issue_keys or any(
                l["status"] == "in_review" for l in laps) or partial:
            status = "IN_REVIEW"
        else:
            status = "DNF"

        results.append({
            "competitor_id": p.id,
            "bib": p.bib, "name": p.name,
            "category_id": p.category_id,
            "category_name": p.category_name,
            "mixed": p.mixed, "class_label": p.class_label,
            "rank_by": p.rank_by, "laps_required": p.laps_required,
            "gun_time": iso(p.gun_time),
            "net_start_time": net_start,
            "net_start_lo": net_start_lo, "net_start_hi": net_start_hi,
            "net_start_basis": net_start_basis,
            "laps": laps, "partial_lap": partial,
            "chip_switches": chip_switches,
            "confirmed_laps": len(confirmed_laps),
            "extra_laps": max(0, len(confirmed_laps) - p.laps_required),
            "final_finish_time": final_finish,
            "total_net_s": total_net_s,
            "total_net_lo": total_net_lo, "total_net_hi": total_net_hi,
            "total_gun_s": total_gun_s,
            "total_gun_lo": total_gun_lo, "total_gun_hi": total_gun_hi,
            "finish_uncertain": finish_uncertain,
            "open_issue_keys": open_issue_keys,
            "stale_issue_keys": stale_issue_keys,
            "status": status,
            "timeline": sorted(
                timeline, key=lambda x: (x["time"], x["read_id"])),
        })

    # reconstruct consumed unknown-chip issues from decision history
    present_keys = {i["issue_key"] for i in all_issues}
    for d in bundle.decisions:
        if d.kind == UNKNOWN_CHIP and d.issue_key not in present_keys:
            chip = d.issue_key.split("CHIP:", 1)[-1]
            chip_reads = [r for r in bundle.reads if r.chip == chip]
            chip_reads.sort(key=lambda x: (point_of(x.id), x.id))
            devs = sorted({r.device_id for r in chip_reads})
            issue = _make_issue(
                d.issue_key, UNKNOWN_CHIP, competitor_id=d.competitor_id,
                read_id=chip_reads[0].id if chip_reads else None,
                devices=devs, cal_signatures=sigs_of_devices(devs),
                detail={
                    "chip": chip,
                    "first_seen": iso(point_of(chip_reads[0].id))
                    if chip_reads else None,
                    "read_count": len(chip_reads),
                    "nodes": sorted({r.node_code for r in chip_reads}),
                    "consumed": True,
                })
            changed = any(_cal_changed(d.cal_context, sigs, dev) for dev in devs)
            issue["context_changed"] = changed
            issue["resolution"] = {
                "decision": d.decision,
                "decision_label": DECISION_LABELS.get(d.decision, d.decision),
                "reason": d.reason, "decided_by": d.decided_by,
                "decided_at": iso(d.decided_at), "payload": d.payload,
                "cal_context": d.cal_context,
                "needs_reverification": changed,
            }
            all_issues.append(issue)
            present_keys.add(d.issue_key)

    all_issues.sort(key=lambda i: (
        i["competitor_id"] is None, i["competitor_id"] or 0,
        i["issue_key"],
    ))

    out = {
        "algo_version": ALGO_VERSION,
        "results": results,
        "issues": all_issues,
        "leaderboard": _leaderboard(results),
    }
    return out


def _metric_bounds(r):
    if r["rank_by"] == "net":
        return r["total_net_lo"], r["total_net_s"], r["total_net_hi"]
    return r["total_gun_lo"], r["total_gun_s"], r["total_gun_hi"]


def _rank_rows(rows, key_extra=None):
    """Assign ranks with interval-aware ties.

    Adjacent finishers (ordered by point estimate) whose uncertainty
    intervals overlap form a tie group — displayed 并列待裁定, never split by
    invented precision.  Exact-equal point estimates also tie.
    """
    ordered = sorted(rows, key=lambda r: (
        (r["total_net_s"] if r["rank_by"] == "net" else r["total_gun_s"]),
        r["bib"]))
    groups: list[list] = []
    for r in ordered:
        lo, pt, hi = _metric_bounds(r)
        if groups:
            prev = groups[-1][-1]
            plo, ppt, phi = _metric_bounds(prev)
            overlap = lo <= phi and plo <= hi
            exact = pt == ppt
            # Certain (zero-width) intervals only tie on exact equality.
            uncertain_overlap = overlap and (
                r.get("finish_uncertain") or prev.get("finish_uncertain"))
            if exact or uncertain_overlap:
                groups[-1].append(r)
                continue
        groups.append([r])

    rank = 1
    out = []
    for g in groups:
        tied = len(g) > 1
        for r in g:
            lo, pt, hi = _metric_bounds(g[0])
            out.append((r, rank, tied))
        rank += len(g)
    return out


def _leaderboard(results):
    boards = []
    by_cat: dict[int, list] = {}
    for r in results:
        by_cat.setdefault(r["category_id"], []).append(r)

    for cat_id, rows in sorted(by_cat.items()):
        ranked = [r for r in rows if r["status"] == "FINISHER"]
        others = [r for r in rows if r["status"] != "FINISHER"]
        mixed = any(r["mixed"] for r in rows)

        entries = []
        overall = {r["competitor_id"]: (rank, tied)
                   for r, rank, tied in _rank_rows(ranked)}
        for r in rows:
            if r["status"] != "FINISHER":
                continue
            rank, tied = overall[r["competitor_id"]]
            lo, pt, hi = _metric_bounds(r)
            entries.append({
                "competitor_id": r["competitor_id"], "bib": r["bib"],
                "name": r["name"], "status": r["status"],
                "class_label": r["class_label"],
                "rank_by": r["rank_by"],
                "laps": r["confirmed_laps"],
                "total_net_s": r["total_net_s"],
                "total_net_lo": r["total_net_lo"],
                "total_net_hi": r["total_net_hi"],
                "total_gun_s": r["total_gun_s"],
                "total_gun_lo": r["total_gun_lo"],
                "total_gun_hi": r["total_gun_hi"],
                "finish_uncertain": r["finish_uncertain"],
                "rank": rank, "tied": tied,
                "tie_label": "并列待裁定" if tied else None,
                "class_rank": None, "class_tied": False,
            })
        if mixed:
            by_class: dict[str, list] = {}
            for r in ranked:
                by_class.setdefault(r["class_label"] or "", []).append(r)
            class_rank = {}
            for label, crows in by_class.items():
                for cr, crk, ct in _rank_rows(crows):
                    class_rank[cr["competitor_id"]] = (crk, ct)
            for e in entries:
                cr = class_rank.get(e["competitor_id"])
                if cr:
                    e["class_rank"], e["class_tied"] = cr
                    if cr[1]:
                        e["class_tie_label"] = "并列待裁定"

        for r in sorted(others, key=lambda x: x["bib"]):
            entries.append({
                "competitor_id": r["competitor_id"], "bib": r["bib"],
                "name": r["name"], "status": r["status"],
                "class_label": r["class_label"],
                "rank_by": r["rank_by"],
                "laps": r["confirmed_laps"],
                "total_net_s": r["total_net_s"],
                "total_net_lo": r["total_net_lo"],
                "total_net_hi": r["total_net_hi"],
                "total_gun_s": r["total_gun_s"],
                "total_gun_lo": r["total_gun_lo"],
                "total_gun_hi": r["total_gun_hi"],
                "finish_uncertain": r["finish_uncertain"],
                "rank": None, "tied": False, "tie_label": None,
                "class_rank": None, "class_tied": False,
            })
        boards.append({"category_id": cat_id, "entries": entries})
    return boards


def replay_with_hash(bundle: Bundle) -> dict:
    out = compute_replay(bundle)
    return {
        "input_hash": input_hash(bundle),
        "output_hash": _hash(out),
        "component_hashes": component_hashes(bundle),
        "output": out,
    }
