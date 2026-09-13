"""Deterministic lap reconstruction.

This module is a *pure* function of its inputs: master data, raw chip reads
and recorded human decisions.  It performs no I/O and never reads the wall
clock, so replaying the same input always yields the same candidate laps,
issues and hashes.

Key modelling choices
----------------------
* Repeated reads of one chip at the same mat within ``repeat_window_s`` are
  a single "read cluster" (athlete standing on the mat).  Only one read can
  stand; which one is an explicit, reviewable decision — never silent.
* A finish read that appears while the next lap's start is still expected is
  a ``REPEAT_FINISH`` question (another lap vs. standing on the finish mat),
  not an automatic lap.
* Missing intermediate nodes and backwards node jumps raise issues.
  Pending issues keep the lap *in review*; the system never voids a lap by
  itself.  An official must credit or disallow each item.
* Chip swaps are resolved before the walk: reads are attributed to the
  stable competitor identity through time-bounded chip assignments, so a
  mid-race chip change forms one continuous timeline.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ALGO_VERSION = "1.1.0"

# How early before the gun a start-mat read is still believable.
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

# Human-readable explanations shown verbatim in the UI ("判定依据可查").
KIND_TITLES = {
    DUP: "同一感应点短时重复读卡（可能站在感应区）",
    MISSING: "漏点：经过下一节点但缺少该节点读卡",
    REVERSE: "逆向记录：节点顺序倒退",
    REPEAT_FINISH: "终点重复读卡：新的一圈还是停留在终点？",
    IMPLAUSIBLE: "用时超出可行区间（过快或超过关门时间）",
    UNKNOWN_NODE: "未知路线节点编号",
    UNKNOWN_CHIP: "芯片未登记到任何选手",
    BEFORE_GUN: "起点读卡早于枪声时间",
    OFFICIAL: "裁判长综合裁定",
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

# Decisions that accept/confirm the contested read, node or lap.
ACCEPTING = {
    "KEEP_FIRST", "KEEP_LATEST", "CREDIT_NODE", "ACCEPT_READ",
    "CREDIT_LAP", "CONFIRM_READ",
}
# Decisions that drop the contested evidence.
REJECTING = {"REJECT_READ", "REJECT_CHIP"}


def utc(dt) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else utc(dt).isoformat()


# ---------------------------------------------------------------------------
# Input types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeT:
    code: str
    name: str
    kind: str  # start | control | finish
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
    decided_at: datetime | None = None


@dataclass
class Bundle:
    repeat_window_s: float
    courses: tuple[CourseT, ...]
    competitors: tuple[CompetitorT, ...]
    reads: tuple[ReadT, ...]
    decisions: tuple[DecisionT, ...] = ()


# ---------------------------------------------------------------------------
# Canonical serialization / hashing
# ---------------------------------------------------------------------------
def _canon(value) -> object:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items()}
    return value


def _dump(value) -> str:
    return json.dumps(
        _canon(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def canonical_input(bundle: Bundle) -> dict:
    return {
        "algo_version": ALGO_VERSION,
        "settings": {"repeat_window_s": bundle.repeat_window_s},
        "courses": [
            {
                "id": c.id,
                "name": c.name,
                "nodes": [
                    {
                        "code": n.code, "name": n.name, "kind": n.kind,
                        "order": n.order, "min_split_s": n.min_split_s,
                        "max_split_s": n.max_split_s,
                    }
                    for n in sorted(c.nodes, key=lambda x: x.order)
                ],
            }
            for c in sorted(bundle.courses, key=lambda x: x.id)
        ],
        "competitors": [
            {
                "id": p.id, "bib": p.bib, "name": p.name,
                "category_id": p.category_id, "category_name": p.category_name,
                "laps_required": p.laps_required, "course_id": p.course_id,
                "gun_time": p.gun_time, "rank_by": p.rank_by,
                "mixed": p.mixed, "class_label": p.class_label,
                "assignments": [
                    {
                        "chip": a.chip, "valid_from": a.valid_from,
                        "valid_to": a.valid_to, "source": a.source,
                        "note": a.note,
                    }
                    for a in sorted(p.assignments, key=lambda x: x.chip)
                ],
            }
            for p in sorted(bundle.competitors, key=lambda x: x.id)
        ],
        "reads": [
            {
                "id": r.id, "chip": r.chip, "node_code": r.node_code,
                "device_id": r.device_id, "raw_seq": r.raw_seq,
                "read_time": r.read_time,
            }
            for r in sorted(bundle.reads, key=lambda x: x.id)
        ],
        "decisions": [
            {
                "id": d.id, "issue_key": d.issue_key, "kind": d.kind,
                "decision": d.decision, "reason": d.reason,
                "decided_by": d.decided_by, "competitor_id": d.competitor_id,
                "payload": d.payload, "decided_at": d.decided_at,
            }
            for d in sorted(bundle.decisions, key=lambda x: x.id)
        ],
    }


def input_hash(bundle: Bundle) -> str:
    return _hash(canonical_input(bundle))


# ---------------------------------------------------------------------------
# Replay helpers
# ---------------------------------------------------------------------------
def _make_issue(key, kind, *, competitor_id, read_id=None, detail=None,
                options=None):
    return {
        "issue_key": key,
        "kind": kind,
        "title": KIND_TITLES[kind],
        "competitor_id": competitor_id,
        "read_id": read_id,
        "detail": detail or {},
        "options": list(options or ALLOWED_DECISIONS[kind]),
        "resolution": None,
    }


def _attributed(competitors, reads):
    """Attribute reads to competitors via time-valid chip assignments."""
    by_id = {p.id: [] for p in competitors}
    unknown = []
    for r in sorted(reads, key=lambda x: (utc(x.read_time), x.id)):
        t = utc(r.read_time)
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


def _tl_entry(r, treatment, issue_key=None):
    return {
        "read_id": r.id, "chip": r.chip, "node_code": r.node_code,
        "time": iso(r.read_time), "device_id": r.device_id,
        "raw_seq": r.raw_seq, "treatment": treatment,
        "issue_key": issue_key,
    }


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def compute_replay(bundle: Bundle) -> dict:
    """Pure replay; see module docstring."""
    courses = {c.id: c for c in bundle.courses}
    per_comp, unknown_reads = _attributed(bundle.competitors, bundle.reads)

    # Latest decision wins per issue key (append-only adjudication history).
    decisions_map: dict[str, DecisionT] = {}
    for d in sorted(bundle.decisions, key=lambda x: x.id):
        decisions_map[d.issue_key] = d

    all_issues: list[dict] = []
    results: list[dict] = []

    # --- unknown chips ---------------------------------------------------
    unknown_by_chip: dict[str, list[ReadT]] = {}
    for r in unknown_reads:
        unknown_by_chip.setdefault(r.chip, []).append(r)
    for chip, group in sorted(unknown_by_chip.items()):
        group.sort(key=lambda x: (utc(x.read_time), x.id))
        all_issues.append(_make_issue(
            f"CHIP:{chip}", UNKNOWN_CHIP, competitor_id=None,
            read_id=group[0].id,
            detail={
                "chip": chip,
                "first_seen": iso(group[0].read_time),
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
        raw = sorted(per_comp[p.id], key=lambda x: (utc(x.read_time), x.id))

        timeline: list[dict] = []
        laps: list[dict] = []
        issues: list[dict] = []

        # ---- collapse same-mat repeat clusters -------------------------
        clusters: list[list[ReadT]] = []
        for r in raw:
            if clusters:
                head = clusters[-1][0]
                if (
                    head.chip == r.chip
                    and head.node_code == r.node_code
                    and (utc(r.read_time) - utc(head.read_time)).total_seconds()
                    <= bundle.repeat_window_s
                ):
                    clusters[-1].append(r)
                    continue
            clusters.append([r])

        # chosen read per cluster + reverse lookup
        chosen_ids: dict[int, tuple[str, bool, list[ReadT]]] = {}
        walk_reads: list[ReadT] = []
        for cluster in clusters:
            key = f"R{cluster[0].id}:DUP"
            if len(cluster) > 1:
                issues.append(_make_issue(
                    key, DUP, competitor_id=p.id, read_id=cluster[0].id,
                    detail={
                        "node_code": cluster[0].node_code,
                        "window_s": bundle.repeat_window_s,
                        "reads": [
                            {
                                "read_id": r.id, "time": iso(r.read_time),
                                "device_id": r.device_id, "chip": r.chip,
                            }
                            for r in cluster
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
        held_orders: set[int] = set()  # positions whose read is awaiting review
        anchor_time = utc(p.gun_time)
        current = {
            "lap_no": lap_no,
            "segments": [],
            "missing_nodes": [],
            "issue_keys": [],
            "start_time": None,
            "lap_start_anchor": utc(p.gun_time),
        }

        def add_segment(node, r, feasible, gap, note=None):
            current["segments"].append({
                "node_code": node.code, "node_name": node.name,
                "kind": node.kind, "time": iso(r.read_time),
                "read_id": r.id, "chip": r.chip, "gap_s": round(gap, 3),
                "feasible": feasible, "note": note,
            })

        for r in walk_reads:
            t = utc(r.read_time)
            dupkey, is_dup, cluster = chosen_ids[r.id]
            # Emit the collapsed duplicate siblings first (chronological).
            if is_dup:
                for sib in cluster:
                    if sib.id != r.id:
                        timeline.append(
                            _tl_entry(sib, "duplicate_dropped", dupkey)
                        )

            node = by_code.get(r.node_code)
            if node is None:
                key = f"R{r.id}:UNKNOWN_NODE"
                issues.append(_make_issue(
                    key, UNKNOWN_NODE, competitor_id=p.id, read_id=r.id,
                    detail={"node_code": r.node_code},
                ))
                timeline.append(
                    _tl_entry(r, "unknown_node_dropped", key)
                )
                continue

            gap = (t - anchor_time).total_seconds()

            if node.order == expected_order:
                block_key = None
                if (
                    node.kind == "start"
                    and current["start_time"] is None
                    and t < utc(p.gun_time) - timedelta(seconds=GUN_TOLERANCE_S)
                ):
                    block_key = f"R{r.id}:BEFORE_GUN"
                    issues.append(_make_issue(
                        block_key, BEFORE_GUN, competitor_id=p.id,
                        read_id=r.id,
                        detail={
                            "gun_time": iso(p.gun_time),
                            "read_time": iso(t),
                            "delta_s": round(
                                (t - utc(p.gun_time)).total_seconds(), 3),
                        },
                    ))
                if block_key is None and not _split_ok(node, gap):
                    block_key = f"R{r.id}:IMPLAUSIBLE"
                    issues.append(_make_issue(
                        block_key, IMPLAUSIBLE, competitor_id=p.id,
                        read_id=r.id,
                        detail={
                            "node_code": node.code, "gap_s": round(gap, 3),
                            "min_split_s": node.min_split_s,
                            "max_split_s": node.max_split_s,
                        },
                    ))

                if block_key is not None:
                    dec = decisions_map.get(block_key)
                    if not (dec and dec.decision in ACCEPTING):
                        if dec and dec.decision in REJECTING:
                            # Officially dropped: that position is now a
                            # genuine missing node, not merely "on hold".
                            held_orders.discard(node.order)
                            mkey = f"R{r.id}:MISS:{node.code}"
                            issues.append(_make_issue(
                                mkey, MISSING, competitor_id=p.id,
                                read_id=r.id,
                                detail={
                                    "node_code": node.code,
                                    "node_name": node.name, "lap_no": lap_no,
                                    "revealed_by_read_id": r.id,
                                    "origin": "rejected_read",
                                },
                            ))
                            current["missing_nodes"].append(node.code)
                            current["issue_keys"].append(mkey)
                        else:
                            held_orders.add(node.order)
                        timeline.append(_tl_entry(
                            r,
                            "rejected" if dec else "held_review",
                            block_key,
                        ))
                        continue
                    current["issue_keys"].append(block_key)

                add_segment(
                    node, r, block_key is None,
                    max(0.0, gap) if node.kind == "start" else gap,
                )
                timeline.append(_tl_entry(r, "accepted", dupkey if is_dup else None))
                if node.kind == "start" and current["start_time"] is None:
                    current["start_time"] = t
                anchor_time = t
                expected_order += 1

            elif node.order > expected_order:
                # Nodes from the expected position (inclusive) up to the
                # observed node (exclusive) were never recorded.
                missing = [n for n in nodes
                           if expected_order <= n.order < node.order]
                repeat_finish = (
                    node.kind == "finish"
                    and expected_order == start_node.order
                    and lap_no > 1
                )

                if repeat_finish:
                    # Whole next lap unrecorded before this finish hit.
                    key = f"R{r.id}:REPEAT_FINISH"
                    issues.append(_make_issue(
                        key, REPEAT_FINISH, competitor_id=p.id, read_id=r.id,
                        detail={
                            "lap_no": lap_no,
                            "missing_nodes": [n.code for n in missing],
                            "gap_from_last_finish_s": round(gap, 3),
                        },
                    ))
                    dec = decisions_map.get(key)
                    if not (dec and dec.decision == "CREDIT_LAP"):
                        timeline.append(_tl_entry(
                            r,
                            "rejected" if dec else "held_review", key,
                        ))
                        continue
                    current["issue_keys"].append(key)
                    # CREDIT_LAP covers the unseen nodes of that lap.
                    current["missing_nodes"] = [n.code for n in missing]
                    feasible = True
                    note = "credited_by_repeat_finish_decision"
                else:
                    note = "forward_jump" if missing else None
                    for m in missing:
                        if m.order in held_orders:
                            # Already raised as IMPLAUSIBLE/BEFORE_GUN and
                            # awaiting a decision; don't double-count it.
                            held_orders.discard(m.order)
                            continue
                        key = f"R{r.id}:MISS:{m.code}"
                        issues.append(_make_issue(
                            key, MISSING, competitor_id=p.id, read_id=r.id,
                            detail={
                                "node_code": m.code, "node_name": m.name,
                                "lap_no": lap_no,
                                "revealed_by_read_id": r.id,
                            },
                        ))
                        current["missing_nodes"].append(m.code)
                        current["issue_keys"].append(key)
                    feasible = not missing

                add_segment(node, r, feasible, gap, note)
                timeline.append(_tl_entry(
                    r, "accepted",
                    f"R{r.id}:REPEAT_FINISH" if repeat_finish else None,
                ))
                anchor_time = t
                expected_order = node.order + 1

            else:
                # Backwards read (reader out of order / stray record).
                key = f"R{r.id}:REVERSE"
                issues.append(_make_issue(
                    key, REVERSE, competitor_id=p.id, read_id=r.id,
                    detail={
                        "node_code": node.code,
                        "expected_order": expected_order,
                        "actual_order": node.order, "lap_no": lap_no,
                    },
                ))
                dec = decisions_map.get(key)
                accepted = bool(dec and dec.decision == "ACCEPT_READ")
                if not accepted:
                    timeline.append(_tl_entry(
                        r, "rejected" if dec else "held_review", key,
                    ))
                    continue
                add_segment(node, r, True, gap, "forced_by_official")
                timeline.append(_tl_entry(r, "accepted", key))
                anchor_time = t
                expected_order = node.order + 1

            # Close lap on an accepted finish.
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
                start_time = current["start_time"]
                laps.append({
                    "lap_no": lap_no,
                    "start_time": iso(start_time),
                    "start_basis": (
                        "start_mat" if start_time
                        else "lap_anchor_missing_start_mat"
                    ),
                    "finish_time": iso(t),
                    "net_s": round(
                        (t - (start_time or current["lap_start_anchor"]))
                        .total_seconds(), 3
                    ),
                    "gun_elapsed_s": round(
                        (t - utc(p.gun_time)).total_seconds(), 3
                    ),
                    "segments": current["segments"],
                    "missing_nodes": current["missing_nodes"],
                    "issue_keys": current["issue_keys"],
                    "status": lap_status,
                })
                lap_no += 1
                expected_order = start_node.order
                held_orders = set()
                current = {
                    "lap_no": lap_no,
                    "segments": [],
                    "missing_nodes": [],
                    "issue_keys": [],
                    "start_time": None,
                    "lap_start_anchor": t,
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

        # Resolve this competitor's issues for the output.
        for issue in issues:
            d = decisions_map.get(issue["issue_key"])
            if d is not None:
                issue["resolution"] = {
                    "decision": d.decision,
                    "decision_label": DECISION_LABELS.get(d.decision, d.decision),
                    "reason": d.reason,
                    "decided_by": d.decided_by,
                    "decided_at": iso(d.decided_at),
                    "payload": d.payload,
                }
        all_issues.extend(issues)

        # ---- totals / status -------------------------------------------
        confirmed_laps = [l for l in laps if l["status"] == "confirmed"]
        open_issue_keys = sorted({
            i["issue_key"] for i in issues if i["resolution"] is None
        })
        official = [
            d for d in bundle.decisions
            if d.kind == OFFICIAL and d.competitor_id == p.id
        ]
        is_dq = bool(official) and official[-1].decision == "DISQUALIFY"

        first_lap = laps[0] if laps else None
        if first_lap and first_lap["start_time"]:
            net_start_dt = datetime.fromisoformat(first_lap["start_time"])
            net_start = first_lap["start_time"]
            net_start_basis = "first_start_mat_read"
        elif first_lap and first_lap["start_basis"] == "lap_anchor_missing_start_mat":
            net_start_dt = utc(p.gun_time)
            net_start = iso(p.gun_time)
            net_start_basis = "gun_fallback_start_mat_missing"
        else:
            seg_start = next(
                (s for s in current["segments"] if s["kind"] == "start"),
                None,
            )
            if seg_start:
                net_start_dt = datetime.fromisoformat(seg_start["time"])
                net_start = seg_start["time"]
                net_start_basis = "first_start_mat_read"
            else:
                net_start_dt = utc(p.gun_time)
                net_start = iso(p.gun_time)
                net_start_basis = "gun_fallback_start_mat_missing"

        final_finish = total_net_s = total_gun_s = None
        if 0 < p.laps_required <= len(confirmed_laps):
            scoring_lap = confirmed_laps[p.laps_required - 1]
            final_finish = scoring_lap["finish_time"]
            total_gun_s = round(
                (utc(final_finish) - utc(p.gun_time)).total_seconds(), 3
            )
            total_net_s = round(
                (utc(final_finish) - net_start_dt).total_seconds(), 3
            )

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
        elif final_finish is not None and not open_issue_keys:
            status = "FINISHER"
        elif final_finish is not None:
            status = "FINISHER_PENDING_REVIEW"
        elif open_issue_keys or any(
                l["status"] == "in_review" for l in laps) or partial:
            status = "IN_REVIEW"
        else:
            status = "DNF"

        results.append({
            "competitor_id": p.id,
            "bib": p.bib,
            "name": p.name,
            "category_id": p.category_id,
            "category_name": p.category_name,
            "mixed": p.mixed,
            "class_label": p.class_label,
            "rank_by": p.rank_by,
            "laps_required": p.laps_required,
            "gun_time": iso(p.gun_time),
            "net_start_time": net_start,
            "net_start_basis": net_start_basis,
            "laps": laps,
            "partial_lap": partial,
            "chip_switches": chip_switches,
            "confirmed_laps": len(confirmed_laps),
            "extra_laps": max(0, len(confirmed_laps) - p.laps_required),
            "final_finish_time": final_finish,
            "total_net_s": total_net_s,
            "total_gun_s": total_gun_s,
            "open_issue_keys": open_issue_keys,
            "status": status,
            "timeline": sorted(
                timeline, key=lambda x: (x["time"], x["read_id"])
            ),
        })

    # Resolve unknown-chip resolutions too, and reconstruct already-consumed
    # unknown-chip issues (e.g. after ATTACH_CHIP) from decision history so
    # the evidence chain stays visible.
    present_keys = {i["issue_key"] for i in all_issues}
    for d in bundle.decisions:
        if d.kind == UNKNOWN_CHIP and d.issue_key not in present_keys:
            chip = d.issue_key.split("CHIP:", 1)[-1]
            chip_reads = [r for r in bundle.reads if r.chip == chip]
            chip_reads.sort(key=lambda x: (utc(x.read_time), x.id))
            all_issues.append(_make_issue(
                d.issue_key, UNKNOWN_CHIP, competitor_id=d.competitor_id,
                read_id=chip_reads[0].id if chip_reads else None,
                detail={
                    "chip": chip,
                    "first_seen": iso(chip_reads[0].read_time)
                    if chip_reads else None,
                    "read_count": len(chip_reads),
                    "nodes": sorted({r.node_code for r in chip_reads}),
                    "consumed": True,
                },
            ))
            present_keys.add(d.issue_key)
    for issue in all_issues:
        if issue["resolution"] is None:
            d = decisions_map.get(issue["issue_key"])
            if d is not None:
                issue["resolution"] = {
                    "decision": d.decision,
                    "decision_label": DECISION_LABELS.get(d.decision, d.decision),
                    "reason": d.reason,
                    "decided_by": d.decided_by,
                    "decided_at": iso(d.decided_at),
                    "payload": d.payload,
                }
    all_issues.sort(key=lambda i: (
        i["competitor_id"] is None, i["competitor_id"] or 0,
        i["issue_key"],
    ))

    return {
        "algo_version": ALGO_VERSION,
        "results": results,
        "issues": all_issues,
        "leaderboard": _leaderboard(results),
    }


def _leaderboard(results):
    """Rank inside each category, plus class rank for mixed groups.

    Only unquestionable finishers take ranked places; everyone else is
    listed unranked with their review status — suspicious data never
    silently removes or fabricates a placing.
    """
    boards = []
    by_cat: dict[int, list] = {}
    for r in results:
        by_cat.setdefault(r["category_id"], []).append(r)

    for cat_id, rows in sorted(by_cat.items()):
        ranked = [r for r in rows if r["status"] == "FINISHER"]
        others = [r for r in rows if r["status"] != "FINISHER"]

        def metric(r):
            return r["total_net_s"] if r["rank_by"] == "net" else r["total_gun_s"]

        ranked.sort(key=lambda r: (metric(r), r["bib"]))
        entries = []
        for pos, r in enumerate(ranked, start=1):
            entries.append({
                "competitor_id": r["competitor_id"], "bib": r["bib"],
                "name": r["name"], "status": r["status"],
                "class_label": r["class_label"],
                "laps": r["confirmed_laps"],
                "total_net_s": r["total_net_s"],
                "total_gun_s": r["total_gun_s"],
                "rank": pos, "class_rank": None,
            })
        if any(r["mixed"] for r in rows):
            by_class: dict[str, int] = {}
            for e in entries:
                label = e["class_label"] or ""
                by_class[label] = by_class.get(label, 0) + 1
                e["class_rank"] = by_class[label]
        for r in sorted(others, key=lambda x: x["bib"]):
            entries.append({
                "competitor_id": r["competitor_id"], "bib": r["bib"],
                "name": r["name"], "status": r["status"],
                "class_label": r["class_label"],
                "laps": r["confirmed_laps"],
                "total_net_s": r["total_net_s"],
                "total_gun_s": r["total_gun_s"],
                "rank": None, "class_rank": None,
            })
        boards.append({"category_id": cat_id, "entries": entries})
    return boards


def replay_with_hash(bundle: Bundle) -> dict:
    out = compute_replay(bundle)
    return {
        "input_hash": input_hash(bundle),
        "output_hash": _hash(out),
        "output": out,
    }
