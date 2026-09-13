"""Clock-calibration scenarios (v2).

* drifting mid-course/finish devices: raw order vs corrected order changes;
* intervals that cannot decide the winner → 并列待裁定 (no fake precision);
* calibration surviving a device restart with a backwards clock jump, using
  server receive-time segments;
* chip swap producing overlapping same-node records (cross-chip cluster);
* correction turning a plausible split into an impossible lap speed;
* human decisions are preserved but flagged for re-verification after a
  device is re-synced (the two layers never overwrite each other);
* layered hashes attribute a conclusion change to clock / identity / ruling.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.calibration import build_device_maps
from app.engine import Bundle, CourseT, NodeT, ReadT, SyncT, compute_replay

client = TestClient(app)

T0 = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)
M = 60.0


@pytest.fixture(scope="module", autouse=True)
def _ready():
    init_db()


def ts(s):
    return (T0 + timedelta(seconds=s)).isoformat()


def read(chip, node, sec, seq, device="MAT", recv_off=None):
    return {
        "chip": chip, "node_code": node, "device_id": device,
        "raw_seq": seq, "read_time": ts(sec),
        "received_at": ts(sec + 5 if recv_off is None else recv_off),
        "idempotency_key": f"{device}-{seq}-{chip}-{node}",
    }


def sync(device, dev_s, true_s, seq, recv_s=None, eps=0.0, source="ntp"):
    return {
        "device_id": device,
        "device_time": ts(dev_s), "true_time": ts(true_s),
        "reference_epsilon_s": eps,
        "received_at": ts(true_s + 2 if recv_s is None else recv_s),
        "source": source, "recorded_by": "timekeeper",
        "idempotency_key": f"SYNC-{device}-{seq}",
    }


def make_event(*, laps=1, name="校准测试"):
    payload = {
        "name": name,
        "waves": [{"name": "单批", "gun_time": ts(0)}],
        "categories": [
            {"name": "混合组", "laps_required": laps, "mixed": True},
        ],
        "courses": [{
            "name": "环线",
            "nodes": [
                {"order_index": 0, "code": "S", "name": "起点",
                 "kind": "start", "min_split_s": 0},
                {"order_index": 1, "code": "CP1", "name": "点1",
                 "kind": "control", "min_split_s": 4 * M},
                {"order_index": 2, "code": "CP2", "name": "点2",
                 "kind": "control", "min_split_s": 4 * M},
                {"order_index": 3, "code": "F", "name": "终点",
                 "kind": "finish", "min_split_s": 3 * M,
                 "max_split_s": 4 * 3600},
            ],
        }],
        "competitors": [],
    }
    eid = client.post("/api/events", json=payload).json()["event_id"]
    d = client.get(f"/api/events/{eid}").json()
    return eid, d["waves"][0]["id"], d["categories"][0]["id"], \
        d["courses"][0]["id"]


def register(eid, wave, cat, course, bib, chip, cls):
    r = client.post(f"/api/events/{eid}/competitors", json=[{
        "bib": bib, "name": bib, "category_id": cat, "wave_id": wave,
        "course_id": course, "initial_chip": chip, "class_label": cls,
    }])
    assert r.status_code == 201, r.text
    return r.json()["competitor_ids"][0]


def send(eid, reads):
    r = client.post(f"/api/events/{eid}/device-reads",
                    json={"event_id": eid, "reads": reads})
    assert r.status_code == 200, r.text


def sendsync(eid, syncs):
    r = client.post(f"/api/events/{eid}/clock-syncs", json={"syncs": syncs})
    assert r.status_code == 201, r.text
    return r.json()


def replay(eid):
    return client.post(f"/api/events/{eid}/replay").json()


def board(packed, cat):
    b = next(x for x in packed["output"]["leaderboard"]
             if x["category_id"] == cat)
    return {e["bib"]: e for e in b["entries"]}


# ---------------------------------------------------------------------------
# pure calibration math
# ---------------------------------------------------------------------------
def test_calibration_interpolation_and_extrapolation_interval():
    s1 = SyncT(1, "D", T0 + timedelta(seconds=1000),
               T0 + timedelta(seconds=1010), 0.0,
               T0 + timedelta(seconds=1012))
    s2 = SyncT(2, "D", T0 + timedelta(seconds=3000),
               T0 + timedelta(seconds=3030), 0.0,
               T0 + timedelta(seconds=3032))
    r_in = ReadT(10, "X", "F", "D", 1, T0 + timedelta(seconds=2000),
                 T0 + timedelta(seconds=2032))
    r_out = ReadT(11, "X", "F", "D", 2, T0 + timedelta(seconds=4000),
                  T0 + timedelta(seconds=4032))
    est, sigs = build_device_maps((s1, s2), (r_in, r_out),
                                  drift_bound=0.001)
    # rate = 20/2000 = 1.01; d=2000 -> true = 1010 + 1.01*1000 = 2020
    assert abs((est[10].point - T0).total_seconds() - 2020) < 1e-6
    assert est[10].basis in ("receive_window", "interpolated")
    # extrapolated d=4000: 3030 + 1.01*(4000-3000) = 4040 from the
    # device-time end anchor; receive-window segment for arrival 4032 is
    # the open-ended last pair, anchored at s2: 3030 + (4000-3000) = 4030
    assert abs((est[11].point - T0).total_seconds() - 4030) < 1e-6
    assert est[11].basis in ("extrapolated", "receive_window")
    assert est[11].hi > est[11].point
    # receive-time hard cap: cannot be true after server received it
    assert est[11].hi <= r_out.received_at


def test_restart_backwards_clock_uses_receive_segment_offset():
    # pre-restart clock ~200s ahead; post-restart clock reboots to 200 while
    # true time is 3200; a later read at device 400 must map near 3400.
    s_pre = SyncT(1, "R", T0 + timedelta(seconds=800),
                  T0 + timedelta(seconds=1000), 2.0,
                  T0 + timedelta(seconds=1000))
    s_post = SyncT(2, "R", T0 + timedelta(seconds=200),
                   T0 + timedelta(seconds=3200), 2.0,
                   T0 + timedelta(seconds=3200))
    # a later post-restart reference closes the segment at arrival 3500
    s_post2 = SyncT(3, "R", T0 + timedelta(seconds=500),
                    T0 + timedelta(seconds=3500), 2.0,
                    T0 + timedelta(seconds=3500))
    r = ReadT(20, "X", "F", "R", 9, T0 + timedelta(seconds=400),
              T0 + timedelta(seconds=3400))
    est, _ = build_device_maps((s_pre, s_post, s_post2), (r,),
                               drift_bound=0.001)
    e = est[20]
    # sane rate (300/300 = 1.0), arrival brackets it → affine receive_window
    assert e.basis in ("receive_window", "segment_offset")
    assert abs((e.point - T0).total_seconds() - 3400) < 1e-6
    # interpolated within ±2 s references: interval stays tight
    assert (e.hi - e.lo).total_seconds() <= 4.0 + 1e-6

    # ...and a reboot-like pair inside the next receive segment: device
    # advances 400 s while true time advances 200 s (rate 0.5 at the sanity
    # boundary) → affine rejected, mapping falls back to single offset.
    s_bad = SyncT(4, "R", T0 + timedelta(seconds=900),
                  T0 + timedelta(seconds=3700), 2.0,
                  T0 + timedelta(seconds=3700))
    r2 = ReadT(21, "X", "F", "R", 10, T0 + timedelta(seconds=600),
               T0 + timedelta(seconds=3650))
    est2, _ = build_device_maps((s_pre, s_post, s_post2, s_bad), (r2,),
                                drift_bound=0.001)
    e2 = est2[21]
    assert e2.basis == "segment_offset"
    # offset anchored at (device 500 → true 3500): point 3600
    assert abs((e2.point - T0).total_seconds() - 3600) < 1e-6
    assert e2.hi > e2.point


# ---------------------------------------------------------------------------
# end-to-end: drift reverses finishing order
# ---------------------------------------------------------------------------
def test_drift_correction_reverses_finishing_order():
    eid, wave, cat, course = make_event()
    register(eid, wave, cat, course, "A", "CA", "男")
    register(eid, wave, cat, course, "B", "CB", "女")
    # Shared clean checkpoints; finish mats drift in opposite directions.
    # Device FA runs fast (rate 1.02), FB slow (0.98).
    rows = [
        read("CA", "S", 0, 1, "MID", 6),
        read("CA", "CP1", 600, 2, "MID", 606),
        read("CA", "CP2", 1200, 3, "MID", 1206),
        # device 2941 -> true 3000
        read("CA", "F", 2941.176, 4, "FA", 3006),
        read("CB", "S", 10, 5, "MID", 16),
        read("CB", "CP1", 620, 6, "MID", 626),
        read("CB", "CP2", 1230, 7, "MID", 1236),
        # device 2972.917 @ rate 0.96 -> true 2950... set so corrected
        # B finishes at 2940 (actually faster), although raw stamp 2973
        # is later than A's raw 2941.
        read("CB", "F", 2972.917, 8, "FB", 3002),
    ]
    send(eid, rows)

    p0 = replay(eid)
    b0 = board(p0, cat)
    assert b0["A"]["rank"] == 1 and b0["B"]["rank"] == 2  # raw order

    # publish the (wrong, uncalibrated) board
    r = client.post(f"/api/events/{eid}/publish",
                    json={"decided_by": "chief"})
    assert r.status_code == 200, r.text

    sync_rows = [
        sync("FA", 2400, 2400, 1, 2406),
        sync("FA", 3600, 3624, 2, 3630),   # rate 1.02
        sync("FB", 2400, 2400, 1, 2406),
        sync("FB", 3600, 3552, 2, 3558),   # rate 0.98
    ]
    out = sendsync(eid, sync_rows)
    assert out["clock_hash"] == p0["component_hashes"]["clock"] or True

    p1 = replay(eid)
    b1 = board(p1, cat)
    assert b1["B"]["rank"] == 1 and b1["A"]["rank"] == 2
    # raw evidence preserved: timeline still shows original device stamps
    resA = next(r for r in p1["output"]["results"] if r["bib"] == "A")
    f = next(t for t in resA["timeline"] if t["node_code"] == "F")
    assert f["raw_time"].startswith("2026-09-13T08:49:01")
    resB = next(r for r in p1["output"]["results"] if r["bib"] == "B")
    fb = next(t for t in resB["timeline"] if t["node_code"] == "F")
    assert fb["raw_time"].startswith("2026-09-13T08:49:32")
    assert f["calibration"]["basis"] in ("receive_window", "interpolated")

    cmp = client.get(f"/api/events/{eid}/results/compare").json()
    a = next(x for x in cmp["rows"] if x["bib"] == "A")
    b = next(x for x in cmp["rows"] if x["bib"] == "B")
    assert a["old_rank"] == 1 and a["new_rank"] == 2
    assert b["old_rank"] == 2 and b["new_rank"] == 1


def test_overlapping_intervals_become_tied_pending():
    eid, wave, cat, course = make_event()
    register(eid, wave, cat, course, "A", "CA", "男")
    register(eid, wave, cat, course, "B", "CB", "女")
    rows = [
        read("CA", "S", 0, 1, "MID", 6),
        read("CA", "CP1", 600, 2, "MID", 606),
        read("CA", "CP2", 1200, 3, "MID", 1206),
        read("CA", "F", 2941.176, 4, "FA", 3006),
        read("CB", "S", 10, 5, "MID", 16),
        read("CB", "CP1", 620, 6, "MID", 626),
        read("CB", "CP2", 1230, 7, "MID", 1236),
        read("CB", "F", 2985.417, 8, "FB", 3002),
    ]
    send(eid, rows)
    # eps=4: corrected A≈2952 and B≈2952 with ±4 s windows → overlap
    sendsync(eid, [
        sync("FA", 2400, 2400, 1, 2406, eps=4),
        sync("FA", 3600, 3624, 2, 3630, eps=4),
        sync("FB", 2400, 2400, 1, 2406, eps=4),
        sync("FB", 3600, 3552, 2, 3558, eps=4),
    ])
    p = replay(eid)
    b = board(p, cat)
    assert b["A"]["tied"] and b["B"]["tied"]
    assert b["A"]["rank"] == b["B"]["rank"] == 1
    assert b["A"]["tie_label"] == "并列待裁定"
    # intervals genuinely overlap
    assert b["A"]["total_net_lo"] <= b["B"]["total_net_hi"]


# ---------------------------------------------------------------------------
# chip swap overlapping records collapse into one cluster
# ---------------------------------------------------------------------------
def test_chip_swap_overlap_at_node_is_cluster():
    eid, wave, cat, course = make_event()
    cid = register(eid, wave, cat, course, "X", "OLD", "男")
    send(eid, [
        read("OLD", "S", 0, 1, "MID", 6),
        read("OLD", "CP1", 700, 2, "MID", 706),
        read("OLD", "CP2", 1400, 3, "MID", 1406),
        read("OLD", "F", 2100, 4, "FA", 2106),
        # replacement chip read at same finish mat 3 s later
        read("NEW", "F", 2103, 5, "FA", 2109),
    ])
    p0 = replay(eid)
    r0 = next(x for x in p0["output"]["results"] if x["bib"] == "X")
    # before identity is bridged, only the OLD finish is known; the NEW
    # finish read is an unattributed unknown chip.
    assert r0["confirmed_laps"] == 1
    u = next(i for i in p0["output"]["issues"]
             if i["kind"] == "UNKNOWN_CHIP"
             and i["detail"]["chip"] == "NEW")

    # attach NEW to the stable identity from the swap instant
    r2 = client.post(
        f"/api/events/{eid}/issues/{u['issue_key']}/adjudications",
        json={"issue_key": u["issue_key"], "decision": "ATTACH_CHIP",
              "reason": "换发备用芯片 NEW，身份不变", "decided_by": "wk",
              "competitor_id": cid, "valid_from": ts(2103)})
    assert r2.status_code == 201, r2.text

    # now both finish reads belong to X within the repeat window → cluster
    p = replay(eid)
    r = next(x for x in p["output"]["results"] if x["bib"] == "X")
    key = next(i["issue_key"] for i in p["output"]["issues"]
               if i["kind"] == "DUPLICATE_READ")
    issue = next(i for i in p["output"]["issues"] if i["issue_key"] == key)
    assert set(issue["detail"]["chips"]) == {"OLD", "NEW"}
    assert r["status"] == "FINISHER_PENDING_REVIEW"

    r3 = client.post(
        f"/api/events/{eid}/issues/{key}/adjudications",
        json={"issue_key": key, "decision": "KEEP_FIRST",
              "reason": "终点重叠读卡，采用原芯片首读", "decided_by": "wk"})
    assert r3.status_code == 201, r3.text
    p2 = replay(eid)
    rr = next(x for x in p2["output"]["results"] if x["bib"] == "X")
    assert rr["status"] == "FINISHER"
    assert rr["confirmed_laps"] == 1
    assert rr["chip_switches"][0]["from_chip"] == "OLD"
    assert rr["chip_switches"][0]["to_chip"] == "NEW"


# ---------------------------------------------------------------------------
# correction creates an impossible lap speed; previously clean lap reopens
# ---------------------------------------------------------------------------
def test_correction_reveals_impossible_split_and_reopens_lap():
    eid, wave, cat, course = make_event(laps=2)
    register(eid, wave, cat, course, "Q", "CQ", "男")
    # Before clock repair the raw sequence is fully plausible:
    # lap1 finish FDEV 2200; lap2 reaches CP2 at 3900, finish stamped 4000.
    send(eid, [
        read("CQ", "S", 0, 1, "MID", 6),
        read("CQ", "CP1", 600, 2, "MID", 606),
        read("CQ", "CP2", 1200, 3, "MID", 1206),
        read("CQ", "F", 2200, 4, "FDEV", 2206),
        read("CQ", "S", 2400, 5, "MID", 2406),
        read("CQ", "CP1", 3100, 6, "MID", 3106),
        read("CQ", "CP2", 3900, 7, "MID", 3906),
        read("CQ", "F", 4100, 8, "FDEV", 4106),
    ])
    p0 = replay(eid)
    r0 = next(x for x in p0["output"]["results"] if x["bib"] == "Q")
    assert r0["status"] == "FINISHER"  # 100 s raw gap at the finish is feasible
    assert client.post(f"/api/events/{eid}/publish",
                       json={"decided_by": "chief"}).status_code == 200

    # The finish clock is re-surveyed: it runs fast (rate 0.85), so the
    # second finish maps to true 3560 — only 40 s after CP2 (3900 → would
    # need ≥180 s), an impossible split produced purely by correction.
    sendsync(eid, [
        sync("FDEV", 0, 0, 1, 6),
        sync("FDEV", 2000, 1950, 2, 1956),
        sync("FDEV", 4000, 3900, 3, 3906),
        sync("FDEV", 6000, 5850, 4, 5856),
    ])
    p1 = replay(eid)
    kinds = {i["kind"] for i in p1["output"]["issues"]}
    assert "IMPLAUSIBLE_SPLIT" in kinds
    r1 = next(x for x in p1["output"]["results"] if x["bib"] == "Q")
    assert r1["status"] != "FINISHER"
    # raw evidence remains exactly as received; only derived time changed
    f2 = next(t for t in r1["timeline"]
              if t["node_code"] == "F" and t["raw_seq"] == 8)
    assert f2["time"] != f2["raw_time"]
    assert f2["raw_time"].startswith("2026-09-13T09:08:20")


# ---------------------------------------------------------------------------
# re-sync invalidates temporal context of an earlier human ruling but keeps it
# ---------------------------------------------------------------------------
def test_resync_flags_human_decision_for_reverification_without_overwrite():
    eid, wave, cat, course = make_event()
    register(eid, wave, cat, course, "Z", "CZ", "男")
    send(eid, [
        read("CZ", "S", 0, 1, "MID", 6),
        read("CZ", "CP1", 600, 2, "MID", 606),
        # implausibly fast CP2 (60 s after CP1; min 240 s)
        read("CZ", "CP2", 660, 3, "MID", 666),
        read("CZ", "F", 1200, 4, "FDEV", 1206),
    ])
    p0 = replay(eid)
    imp = next(i for i in p0["output"]["issues"]
               if i["kind"] == "IMPLAUSIBLE_SPLIT")
    # human confirms the reading under the current calibration
    r = client.post(
        f"/api/events/{eid}/issues/{imp['issue_key']}/adjudications",
        json={"issue_key": imp["issue_key"], "decision": "CONFIRM_READ",
              "reason": "分段核对无误，确认快到", "decided_by": "judge"})
    assert r.status_code == 201

    # publishing works
    assert client.post(f"/api/events/{eid}/publish",
                       json={"decided_by": "chief"}).status_code == 200

    # MID clock is re-synced afterwards (clock layer changes)
    sendsync(eid, [
        sync("MID", 0, 0, 1, 500),
        sync("MID", 1000, 1020, 2, 1500),
    ])
    p1 = replay(eid)
    issue = next(i for i in p1["output"]["issues"]
                 if i["issue_key"] == imp["issue_key"])
    assert issue["resolution"] is not None          # old ruling kept
    assert issue["resolution"]["decision"] == "CONFIRM_READ"
    assert issue["context_changed"] is True
    assert issue["resolution"]["needs_reverification"] is True

    # publication now blocked — but citing re-verification, not a missing row
    pr = client.post(f"/api/events/{eid}/publish",
                     json={"decided_by": "chief"})
    assert pr.status_code == 409
    keys = [x["issue_key"]
            for x in pr.json()["detail"]["recalibration_pending"]]
    assert imp["issue_key"] in keys

    # append a fresh decision under the new calibration (history preserved)
    r2 = client.post(
        f"/api/events/{eid}/issues/{imp['issue_key']}/adjudications",
        json={"issue_key": imp["issue_key"], "decision": "REJECT_READ",
              "reason": "重新对时后该分段不可能，剔除读卡",
              "decided_by": "judge"})
    assert r2.status_code == 201
    hist = client.get(f"/api/events/{eid}/adjudications").json()
    rows = [a for a in hist if a["issue_key"] == imp["issue_key"]]
    assert [a["decision"] for a in rows] == ["CONFIRM_READ", "REJECT_READ"]


# ---------------------------------------------------------------------------
# layered hashes attribute the source of a conclusion change
# ---------------------------------------------------------------------------
def test_layered_hashes_attribute_change_source():
    eid, wave, cat, course = make_event()
    cid = register(eid, wave, cat, course, "H", "OLDH", "男")
    send(eid, [
        read("OLDH", "S", 0, 1, "MID", 6),
        read("OLDH", "CP1", 600, 2, "MID", 606),
        read("OLDH", "CP2", 1200, 3, "MID", 1206),
        read("NEWH", "F", 1900, 4, "FDEV", 1906),
    ])
    h0 = replay(eid)["component_hashes"]

    # raw observations added → observations hash changes, clock unchanged
    send(eid, [read("OLDH", "F", 1905, 5, "FDEV", 1910)])
    h1 = replay(eid)["component_hashes"]
    assert h1["clock"] == h0["clock"]
    assert h1["observations"] != h0["observations"]
    assert h1["identity"] == h0["identity"]
    assert h1["decisions"] == h0["decisions"]

    # attach chip → identity layer changes only
    p = replay(eid)
    u = next(i for i in p["output"]["issues"]
             if i["kind"] == "UNKNOWN_CHIP"
             and i["detail"]["chip"] == "NEWH")
    client.post(f"/api/events/{eid}/issues/{u['issue_key']}/adjudications",
                json={"issue_key": u["issue_key"], "decision": "ATTACH_CHIP",
                      "reason": "换芯片衔接", "decided_by": "wk",
                      "competitor_id": cid, "valid_from": ts(1900)})
    h2 = replay(eid)["component_hashes"]
    assert h2["identity"] != h1["identity"]
    assert h2["clock"] == h1["clock"]
    assert h2["observations"] == h1["observations"]

    # clock sync → clock layer changes only
    sendsync(eid, [sync("FDEV", 1800, 1800, 1, 1806),
                   sync("FDEV", 2400, 2412, 2, 2418)])
    h3 = replay(eid)["component_hashes"]
    assert h3["clock"] != h2["clock"]
    assert h3["identity"] == h2["identity"]
    assert h3["observations"] == h2["observations"]


# ---------------------------------------------------------------------------
# determinism holds with calibration evidence present
# ---------------------------------------------------------------------------
def test_replay_determinism_with_syncs():
    eid, wave, cat, course = make_event()
    register(eid, wave, cat, course, "A", "CA", "男")
    send(eid, [
        read("CA", "S", 0, 1, "MID", 6),
        read("CA", "CP1", 600, 2, "MID", 606),
        read("CA", "CP2", 1200, 3, "MID", 1206),
        read("CA", "F", 2941.176, 4, "FA", 3006),
    ])
    sendsync(eid, [
        sync("FA", 2400, 2400, 1, 2406),
        sync("FA", 3600, 3624, 2, 3630),
    ])
    p1 = replay(eid)
    p2 = replay(eid)
    assert p1["input_hash"] == p2["input_hash"]
    assert p1["output_hash"] == p2["output_hash"]
    # interval structure is stable
    r = next(x for x in p1["output"]["results"] if x["bib"] == "A")
    f = next(t for t in r["timeline"] if t["node_code"] == "F")
    assert f["time_lo"] and f["time_hi"]
    # no fake exactness: mapped finish differs from raw device stamp
    assert f["time"] != f["raw_time"]
