from datetime import datetime, timezone

import pytest

from bfp.schedule import Slot, current_slot, expected_slots, manual_slot, run_done, shop_done


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "when,expected",
    [
        # summer time (CEST, UTC+2) — until 25 Oct 2026
        ("2026-10-10 06:00", "2026-10-10T08"),
        ("2026-10-10 05:59", None),
        ("2026-10-10 07:00", "2026-10-10T08"),   # the second cron firing: same slot (gate dedups)
        ("2026-10-10 08:40", "2026-10-10T08"),   # backup firing still inside the window
        ("2026-10-10 18:00", "2026-10-10T20"),
        ("2026-10-10 21:29", "2026-10-10T20"),
        ("2026-10-10 21:31", None),              # window closed at 23:30 Rome
        # DST ends Sun 25 Oct 2026 03:00 CEST -> 02:00 CET
        ("2026-10-25 06:00", None),              # 07:00 CET: too early
        ("2026-10-25 07:00", "2026-10-25T08"),
        ("2026-10-28 07:00", "2026-10-28T08"),
        ("2026-10-28 19:00", "2026-10-28T20"),
        ("2026-11-27 07:05", "2026-11-27T08"),   # Black Friday
        ("2026-11-27 20:40", "2026-11-27T20"),   # winter backup firing 21:40 CET
    ],
)
def test_current_slot(settings, when, expected):
    s = current_slot(settings, utc(when))
    assert (s.label if s else None) == expected


def test_every_cron_firing_maps_to_each_slot_exactly_once(settings):
    """The workflow cron set must hit every slot both in CEST and CET."""
    crons = [f"{h:02d}:17" for h in (6, 7, 8, 9, 18, 19, 20, 21)]
    for day in ("2026-10-12", "2026-11-12"):
        labels = {current_slot(settings, utc(f"{day} {t}")) for t in crons} - {None}
        assert {s.label for s in labels} == {f"{day}T08", f"{day}T20"}


def test_dst_start_without_end_date(settings):
    settings.end_date = None
    assert current_slot(settings, utc("2027-03-28 06:00")).label == "2027-03-28T08"  # CEST again
    assert current_slot(settings, utc("2027-03-28 07:05")).label == "2027-03-28T08"


def test_no_slots_after_end_date(settings):
    assert current_slot(settings, utc("2026-12-01 19:05")).label == "2026-12-01T20"
    assert current_slot(settings, utc("2026-12-02 07:05")) is None
    assert expected_slots(settings, utc("2026-12-05 12:00"))[-1] == "2026-12-01T20"


def test_manual_slot(settings):
    s = manual_slot(settings, utc("2026-10-02 13:30"))
    assert s.label == "2026-10-02T1530m" and not s.scheduled and s.file_prefix == "1530m"


def test_done_markers(tmp_path):
    slot = Slot("2026-10-10T08", datetime(2026, 10, 10).date(), True)
    assert not run_done(tmp_path, slot, "actions")
    (tmp_path / "2026-10-10T08__actions.json").write_text("{}")
    assert run_done(tmp_path, slot, "actions") and not run_done(tmp_path, slot, "local")
    d = tmp_path / "date=2026-10-10"
    d.mkdir()
    (d / "T08__actions__shop.parquet").write_text("")
    assert not shop_done(tmp_path, slot, "actions", "shop")  # prefix is '08'
    (d / "08__actions__shop.parquet").write_text("")
    assert shop_done(tmp_path, slot, "actions", "shop")


def test_expected_slots(settings):
    slots = expected_slots(settings, utc("2026-10-11 19:00"))
    assert slots == ["2026-10-10T08", "2026-10-10T20", "2026-10-11T08"]
