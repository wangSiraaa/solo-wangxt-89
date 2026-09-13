"""End-to-end scenario tests against a real PostgreSQL database.

Scenarios required by the brief:
1. MIXED category ranking (overall + per sub-class), with staggered waves
   so net time and gun elapsed time genuinely differ.
2. Repeated finish reads: same chip read again on the finish mat — either
   the athlete standing on the mat (held as duplicate cluster) or a real
   extra lap (credited only by explicit adjudication).
3. Mid-race chip swap: reads continue under the stable athlete identity.
Plus reverse-order records, missing nodes, and deterministic replay.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import ResultSnapshot

client = TestClient(app)

T0 = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)
H = 3600.0
M = 60.0


@pytest.fixture(scope="module", autouse=True)
def _ready():
    init_db()


def _ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _read(chip, node, sec, seq, device="MAT"):
    return {
        "chip": chip, "node_code": node, "device_id": device,
        "raw_seq": seq, "read_time": _ts(sec),
        "idempotency_key": f"{device}-{seq}-{chip}-{node}",
    }


def _skeleton():
    return {
        "name": "混合组别耐力赛 2026",
        "waves": [
            {"name": "A批 08:00", "gun_time": _ts(0)},
            {"name": "B批 08:15", "gun_time": _ts(15 * M)},
        ],
        "categories": [
            {"name": "混合公开组", "laps_required": 2, "mixed": True},
            {"name": "女子组", "laps_required": 2, "mixed": False,
             "rank_by": "gun"},
            {"name": "短距离组", "laps_required": 1},
        ],
        "courses": [
            {
                "name": "主环线 S-C1-C2-F",
                "nodes": [
                    {"order_index": 0, "code": "S", "name": "起点/换圈",
                     "kind": "start", "min_split_s": 0},
                    {"order_index": 1, "code": "C1", "name": "打卡点1",
                     "kind": "control", "min_split_s": 5 * M},
                    {"order_index": 2, "code": "C2", "name": "打卡点2",
                     "kind": "control", "min_split_s": 4 * M},
                    {"order_index": 3, "code": "F", "name": "终点",
                     "kind": "finish", "min_split_s": 3 * M,
                     "max_split_s": 4 * H},
                ],
            }
        ],
        "competitors": [],
    }


@pytest.fixture()
def event():
    r = client.post("/api/events", json=_skeleton())
    assert r.status_code == 201, r.text
    event_id = r.json()["event_id"]
    detail = client.get(f"/api/events/{event_id}").json()
    wa = next(w["id"] for w in detail["waves"] if w["name"].startswith("A批"))
    wb = next(w["id"] for w in detail["waves"] if w["name"].startswith("B批"))
    cmix = next(c["id"] for c in detail["categories"] if c["name"] == "混合公开组")
    cwom = next(c["id"] for c in detail["categories"] if c["name"] == "女子组")
    cshort = next(c["id"] for c in detail["categories"] if c["name"] == "短距离组")
    course = detail["courses"][0]["id"]

    regs = [
        {"bib": "101", "name": "甲(男精英)", "category_id": cmix,
         "wave_id": wa, "course_id": course, "initial_chip": "AAA",
         "class_label": "男子精英"},
        {"bib": "102", "name": "乙(女,混开)", "category_id": cmix,
         "wave_id": wb, "course_id": course, "initial_chip": "BBB",
         "class_label": "女子"},
        {"bib": "201", "name": "丙(女子组)", "category_id": cwom,
         "wave_id": wb, "course_id": course, "initial_chip": "CCC"},
        {"bib": "301", "name": "丁(短距离)", "category_id": cshort,
         "wave_id": wa, "course_id": course, "initial_chip": "DDD",
         "class_label": "男子精英"},
    ]
    rr = client.post(f"/api/events/{event_id}/competitors", json=regs)
    assert rr.status_code == 201, rr.text

    detail = client.get(f"/api/events/{event_id}").json()
    ids = {
        "event_id": event_id,
        "wave": {"A": wa, "B": wb},
        "category": {"mix": cmix, "women": cwom, "short": cshort},
        "course": course,
        "competitor": {p["bib"]: p["id"] for p in detail["competitors"]},
    }
    yield ids


def _send(event, reads):
    eid = event["event_id"]
    r = client.post(f"/api/events/{eid}/device-reads",
                    json={"event_id": eid, "reads": reads})
    assert r.status_code == 200, r.text
    return r.json()


def _replay(event):
    r = client.post(f"/api/events/{event['event_id']}/replay")
    assert r.status_code == 200, r.text
    return r.json()


def _result(packed, bib):
    return next(r for r in packed["output"]["results"] if r["bib"] == bib)


def _issues_of(packed, bib, kind):
    cid = next(r["competitor_id"]
               for r in packed["output"]["results"] if r["bib"] == bib)
    return [i for i in packed["output"]["issues"]
            if i["competitor_id"] == cid and i["kind"] == kind]


def _decide(event, key, decision, reason="人工复核录像确认", by="judge-li"):
    r = client.post(
        f"/api/events/{event['event_id']}/issues/{key}/adjudications",
        json={"issue_key": key, "decision": decision, "reason": reason,
              "decided_by": by},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. clean data + mixed-group ranking + net vs gun separation
# ---------------------------------------------------------------------------
def test_mixed_group_net_and_gun_times(event):
    # 101 wave A gun=0, crosses the start mat exactly at the gun.
    # 102 wave B gun=900 but crosses the start mat 60 s late (start crush):
    # net time starts from her mat read; gun elapsed starts from the gun,
    # so gun_elapsed - net = 60 s.
    reads = [
        _read("AAA", "S", 0, 1), _read("AAA", "C1", 600, 2),
        _read("AAA", "C2", 1200, 3), _read("AAA", "F", 2100, 4),
        _read("AAA", "S", 2150, 5), _read("AAA", "C1", 2800, 6),
        _read("AAA", "C2", 3500, 7), _read("AAA", "F", 4200, 8),

        _read("BBB", "S", 960, 10), _read("BBB", "C1", 1800, 11),
        _read("BBB", "C2", 2800, 12), _read("BBB", "F", 3600, 13),
        _read("BBB", "S", 3660, 14), _read("BBB", "C1", 4600, 15),
        _read("BBB", "C2", 5500, 16), _read("BBB", "F", 5700, 17),
    ]
    _send(event, reads)
    packed = _replay(event)
    r101 = _result(packed, "101")
    r102 = _result(packed, "102")

    assert r101["status"] == "FINISHER"
    assert r101["confirmed_laps"] == 2
    assert r101["total_net_s"] == 4200.0
    assert r101["total_gun_s"] == 4200.0
    assert r102["status"] == "FINISHER"
    # net = 5700 - 960; gun elapsed = 5700 - 900
    assert r102["total_net_s"] == 4740.0
    assert r102["total_gun_s"] == 4800.0
    assert r102["net_start_time"].endswith("+00:00")
    assert r102["net_start_basis"] == "first_start_mat_read"

    board = next(b for b in packed["output"]["leaderboard"]
                 if b["category_id"] == event["category"]["mix"])
    by_bib = {e["bib"]: e for e in board["entries"]}
    assert by_bib["101"]["rank"] == 1
    assert by_bib["102"]["rank"] == 2
    # sub-class ranks inside the mixed group
    assert by_bib["101"]["class_rank"] == 1
    assert by_bib["102"]["class_rank"] == 1


def test_gun_ranking_category(event):
    # Women's category ranks by gun elapsed time.
    reads = [
        _read("CCC", "S", 900, 20), _read("CCC", "C1", 1900, 21),
        _read("CCC", "C2", 3000, 22), _read("CCC", "F", 3600, 23),
        _read("CCC", "S", 3650, 24), _read("CCC", "C1", 4500, 25),
        _read("CCC", "C2", 5400, 26), _read("CCC", "F", 5700, 27),
    ]
    _send(event, reads)
    packed = _replay(event)
    r201 = _result(packed, "201")
    assert r201["status"] == "FINISHER"
    assert r201["total_gun_s"] == 4800.0
    board = next(b for b in packed["output"]["leaderboard"]
                 if b["category_id"] == event["category"]["women"])
    assert board["entries"][0]["bib"] == "201"
    assert board["entries"][0]["rank"] == 1


# ---------------------------------------------------------------------------
# 2. finish repeat: standing on the mat vs a genuine extra lap
# ---------------------------------------------------------------------------
def test_finish_duplicate_standing_on_mat(event):
    # 301: one required lap. At the finish there is a tight 4-second repeat
    # cluster (athlete standing on the mat).  Nothing is auto-cancelled;
    # the cluster is raised as an issue and stays visible after deciding.
    reads = [
        _read("DDD", "S", 0, 30), _read("DDD", "C1", 800, 31),
        _read("DDD", "C2", 1700, 32),
        _read("DDD", "F", 2400, 33),
        _read("DDD", "F", 2404, 34),  # same mat, 4 s later
    ]
    _send(event, reads)
    packed = _replay(event)
    r301 = _result(packed, "301")
    dup = _issues_of(packed, "301", "DUPLICATE_READ")
    assert len(dup) == 1
    assert dup[0]["resolution"] is None
    # Lap itself is reconstructable; only the cluster choice is open.
    assert r301["confirmed_laps"] == 1
    dropped = [t for t in r301["timeline"]
               if t["treatment"] == "duplicate_dropped"]
    assert len(dropped) == 1  # evidence kept, marked collapsed pre-decision

    # Official checks video: no second lap, athlete simply stood on mat →
    # keep first read.
    _decide(event, dup[0]["issue_key"], "KEEP_FIRST",
            reason="终点录像确认选手原地停留，未再进入赛道")
    packed2 = _replay(event)
    r301b = _result(packed2, "301")
    dup2 = _issues_of(packed2, "301", "DUPLICATE_READ")
    assert dup2[0]["resolution"]["decision"] == "KEEP_FIRST"
    assert r301b["status"] == "FINISHER"
    assert r301b["confirmed_laps"] == 1
    assert r301b["total_net_s"] == 2400.0


def test_finish_repeat_real_extra_lap_credited(event):
    # 101 has 2 clean laps, then the finish mat fires 37 min later with no
    # S/C1/C2 reads: candidate lap #3 held as REPEAT_FINISH.
    reads = [
        _read("AAA", "S", 0, 40), _read("AAA", "C1", 600, 41),
        _read("AAA", "C2", 1200, 42), _read("AAA", "F", 2100, 43),
        _read("AAA", "S", 2150, 44), _read("AAA", "C1", 2800, 45),
        _read("AAA", "C2", 3500, 46), _read("AAA", "F", 4200, 47),
        _read("AAA", "F", 6420, 48),
    ]
    _send(event, reads)
    packed = _replay(event)
    rf = _issues_of(packed, "101", "REPEAT_FINISH")
    assert len(rf) == 1
    r101 = _result(packed, "101")
    # Required laps exist but the candidate 3rd lap is unresolved → review.
    assert r101["status"] == "FINISHER_PENDING_REVIEW"
    assert r101["confirmed_laps"] == 2

    # Wrong choice (standing on mat) is also supported: reject the read.
    _decide(event, rf[0]["issue_key"], "REJECT_READ",
            reason="终点设备重发，选手当时在休息区")
    packed_rej = _replay(event)
    assert _result(packed_rej, "101")["confirmed_laps"] == 2
    # The late read is visibly rejected in the timeline, not deleted.
    tl = _result(packed_rej, "101")["timeline"]
    late = next(t for t in tl if t["raw_seq"] == 48 and t["node_code"] == "F")
    assert late["treatment"] == "rejected"
    # ... but the raw record itself remains in the immutable feed.
    assert late["read_id"] > 0

    # Re-deciding appends a new adjudication; latest decision wins.
    _decide(event, rf[0]["issue_key"], "CREDIT_LAP",
            reason="裁判组与点位裁判确认确实又完成一圈，途中点漏打卡")
    packed2 = _replay(event)
    r101b = _result(packed2, "101")
    assert r101b["confirmed_laps"] == 3
    assert r101b["extra_laps"] == 1
    assert r101b["status"] == "FINISHER"
    lap3 = r101b["laps"][2]
    assert lap3["status"] == "confirmed"
    assert set(lap3["missing_nodes"]) == {"S", "C1", "C2"}
    # adjudication history preserved (two rows on the same issue key)
    hist = client.get(
        f"/api/events/{event['event_id']}/adjudications").json()
    rows = [a for a in hist if a["issue_key"] == rf[0]["issue_key"]]
    assert [a["decision"] for a in rows] == ["REJECT_READ", "CREDIT_LAP"]


# ---------------------------------------------------------------------------
# 3. mid-race chip swap
# ---------------------------------------------------------------------------
def test_chip_swap_bridges_identity(event):
    reads = [
        _read("AAA", "S", 0, 50), _read("AAA", "C1", 600, 51),
        _read("AAA", "C2", 1200, 52), _read("AAA", "F", 2100, 53),
        _read("AAX", "S", 2150, 54), _read("AAX", "C1", 2800, 55),
        _read("AAX", "C2", 3500, 56), _read("AAX", "F", 4200, 57),
    ]
    _send(event, reads)
    packed = _replay(event)

    # Before the swap is recorded: only one lap attributed, chip unknown.
    r101 = _result(packed, "101")
    assert r101["confirmed_laps"] == 1
    unk = [i for i in packed["output"]["issues"]
           if i["kind"] == "UNKNOWN_CHIP"
           and i["detail"].get("chip") == "AAX"]
    assert len(unk) == 1

    # Official attaches the new chip to the stable competitor identity.
    key = unk[0]["issue_key"]
    r = client.post(
        f"/api/events/{event['event_id']}/issues/{key}/adjudications",
        json={"issue_key": key, "decision": "ATTACH_CHIP",
              "reason": "检录处记录：换发备用芯片 AAX，选手身份不变",
              "decided_by": "judge-wang",
              "competitor_id": event["competitor"]["101"],
              "valid_from": _ts(2150)},
    )
    assert r.status_code == 201, r.text

    packed2 = _replay(event)
    r101b = _result(packed2, "101")
    assert r101b["status"] == "FINISHER"
    assert r101b["confirmed_laps"] == 2
    assert r101b["total_net_s"] == 4200.0
    switches = r101b["chip_switches"]
    assert len(switches) == 1
    assert switches[0]["from_chip"] == "AAA"
    assert switches[0]["to_chip"] == "AAX"
    # Unknown-chip issue now carries the recorded resolution.
    unk2 = next(i for i in packed2["output"]["issues"]
                if i["issue_key"] == key)
    assert unk2["resolution"]["decision"] == "ATTACH_CHIP"


# ---------------------------------------------------------------------------
# 4. reverse record + missing node go to review, never auto-cancel
# ---------------------------------------------------------------------------
def test_reverse_and_missing_go_to_review(event):
    reads = [
        _read("BBB", "S", 960, 60),
        _read("BBB", "C2", 1900, 61),  # forward jump: C1 missing
        _read("BBB", "C1", 2400, 62),  # then backwards record
        _read("BBB", "F", 3000, 63),
    ]
    _send(event, reads)
    packed = _replay(event)
    r102 = _result(packed, "102")
    assert r102["status"] == "IN_REVIEW"
    missing = _issues_of(packed, "102", "MISSING_NODE")
    reverse = _issues_of(packed, "102", "REVERSE_ORDER")
    assert len(missing) == 1 and missing[0]["detail"]["node_code"] == "C1"
    assert len(reverse) == 1
    assert r102["laps"][0]["status"] == "in_review"

    # Publishing while unresolved must be refused.
    r = client.post(f"/api/events/{event['event_id']}/publish",
                    json={"decided_by": "chief"})
    assert r.status_code == 409
    keys = {i["issue_key"] for i in r.json()["detail"]["open_issues"]}
    assert missing[0]["issue_key"] in keys
    assert reverse[0]["issue_key"] in keys

    # C1 physically passed (manual witness), backwards stray read dropped.
    _decide(event, missing[0]["issue_key"], "CREDIT_NODE",
            reason="点位裁判确认选手经过 C1，设备漏读")
    _decide(event, reverse[0]["issue_key"], "REJECT_READ",
            reason="顺序倒退，判为设备重发的杂散记录")
    packed2 = _replay(event)
    r102b = _result(packed2, "102")
    # Lap 1 confirmed; the athlete still needs lap 2 (DNF), but lap 1 is
    # never removed.
    assert r102b["laps"][0]["status"] == "confirmed"
    assert r102b["status"] == "DNF"
    assert r102b["final_finish_time"] is None  # no fabricated finish


# ---------------------------------------------------------------------------
# 5. implausible split needs explicit confirmation
# ---------------------------------------------------------------------------
def test_implausible_split_held_then_confirmed(event):
    # 301 flies C1->C2 in 60 s although the feasible minimum is 4 minutes.
    reads = [
        _read("DDD", "S", 0, 65), _read("DDD", "C1", 800, 66),
        _read("DDD", "C2", 860, 67), _read("DDD", "F", 1500, 68),
    ]
    _send(event, reads)
    packed = _replay(event)
    imp = _issues_of(packed, "301", "IMPLAUSIBLE_SPLIT")
    assert len(imp) == 1
    r301 = _result(packed, "301")
    # Held read does not advance the walk: F then looks like a forward jump,
    # and nobody is declared a finisher automatically.
    assert r301["status"] in ("IN_REVIEW", "FINISHER_PENDING_REVIEW")

    _decide(event, imp[0]["issue_key"], "CONFIRM_READ",
            reason="分段计时核对无误，该选手下坡纪录确实如此")
    packed2 = _replay(event)
    r301b = _result(packed2, "301")
    # Confirming C2 reveals the missing-node handling for the following F;
    # either way it must still be a visible review chain, never silent.
    assert any(
        i["resolution"] and i["resolution"]["decision"] == "CONFIRM_READ"
        for i in packed2["output"]["issues"]
        if i["competitor_id"] == event["competitor"]["301"]
    )


# ---------------------------------------------------------------------------
# 6. determinism: replaying the same input yields identical output
# ---------------------------------------------------------------------------
def test_replay_is_deterministic(event):
    reads = [
        _read("AAA", "S", 0, 70), _read("AAA", "C1", 600, 71),
        _read("AAA", "C2", 1200, 72),
        _read("AAA", "F", 2100, 73), _read("AAA", "F", 2105, 74),
    ]
    _send(event, reads)
    p1 = _replay(event)
    p2 = _replay(event)
    assert p1["input_hash"] == p2["input_hash"]
    assert p1["output_hash"] == p2["output_hash"]

    # Re-sending the identical simulator batch is a no-op.
    again = _send(event, reads)
    assert again["inserted"] == 0
    p3 = _replay(event)
    assert p3["input_hash"] == p1["input_hash"]
    assert p3["output_hash"] == p1["output_hash"]

    dup = next(i for i in p1["output"]["issues"]
               if i["kind"] == "DUPLICATE_READ")
    _decide(event, dup["issue_key"], "KEEP_LATEST")
    p4 = _replay(event)
    p5 = _replay(event)
    assert p4["input_hash"] == p5["input_hash"]
    assert p4["output_hash"] == p5["output_hash"]
    r101 = next(r for r in p4["output"]["results"] if r["bib"] == "101")
    # KEEP_LATEST: the effective finish read is the later one (raw_seq 74),
    # identified stably by node+time rather than DB id.
    finish_seg = r101["laps"][0]["segments"][-1]
    assert finish_seg["node_code"] == "F"
    assert finish_seg["time"] == _ts(2105)


# ---------------------------------------------------------------------------
# 7. publish: snapshots are generated; no way to hand-edit a time
# ---------------------------------------------------------------------------
def test_publish_generates_snapshots(event):
    reads = [
        _read("AAA", "S", 0, 80), _read("AAA", "C1", 600, 81),
        _read("AAA", "C2", 1200, 82), _read("AAA", "F", 2100, 83),
        _read("AAA", "S", 2150, 84), _read("AAA", "C1", 2800, 85),
        _read("AAA", "C2", 3500, 86), _read("AAA", "F", 4200, 87),
    ]
    _send(event, reads)
    r = client.post(f"/api/events/{event['event_id']}/publish",
                    json={"decided_by": "chief"})
    assert r.status_code == 200, r.text
    version = r.json()["version"]

    pub = client.get(
        f"/api/events/{event['event_id']}/results/published").json()
    assert pub["version"] == version
    e101 = next(e for e in pub["entries"] if e["bib"] == "101")
    assert e101["total_net_s"] == 4200.0
    assert e101["laps_confirmed"] == 2
    assert e101["net_start_basis"] == "first_start_mat_read"

    with SessionLocal() as s:
        snap = s.query(ResultSnapshot).filter_by(
            event_id=event["event_id"],
            competitor_id=e101["competitor_id"],
            published=True).one()
        assert snap.input_hash == pub["input_hash"]
        assert snap.basis["total_net_s"] == 4200.0


def test_disqualification_is_recorded_not_a_time_edit(event):
    reads = [
        _read("AAA", "S", 0, 90), _read("AAA", "C1", 600, 91),
        _read("AAA", "C2", 1200, 92), _read("AAA", "F", 2100, 93),
        _read("AAA", "S", 2150, 94), _read("AAA", "C1", 2800, 95),
        _read("AAA", "C2", 3500, 96), _read("AAA", "F", 4200, 97),
    ]
    _send(event, reads)
    r = client.post(
        f"/api/competitors/{event['competitor']['101']}/official-adjudication",
        json={"issue_key": "manual", "decision": "DISQUALIFY",
              "reason": "查实抄近道，裁判长取消成绩", "decided_by": "chief"},
    )
    assert r.status_code == 201, r.text
    packed = _replay(event)
    r101 = _result(packed, "101")
    assert r101["status"] == "DQ"
    board = next(b for b in packed["output"]["leaderboard"]
                 if b["category_id"] == event["category"]["mix"])
    dq_entry = next(e for e in board["entries"] if e["bib"] == "101")
    assert dq_entry["rank"] is None  # removed from ranking, evidence kept
    # DQ is still publishable as an official outcome
    pr = client.post(f"/api/events/{event['event_id']}/publish",
                     json={"decided_by": "chief"})
    assert pr.status_code == 200, pr.text
    pub = client.get(
        f"/api/events/{event['event_id']}/results/published").json()
    e101 = next(e for e in pub["entries"] if e["bib"] == "101")
    assert e101["status"] == "DQ"
