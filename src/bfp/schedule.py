"""Europe/Rome slots, the cron gate, and missing-slot detection.

GitHub cron is UTC-only, so the workflow fires at every UTC hour that can be
08:00/20:00 in Rome (CEST or CET) plus backup times; `gate` decides whether this
firing should collect: local time inside a slot window and slot not yet done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bfp.config import Settings


@dataclass(frozen=True)
class Slot:
    label: str  # "2026-10-10T08" for scheduled slots, "2026-10-02T1530m" for manual runs
    local_date: date
    scheduled: bool

    @property
    def file_prefix(self) -> str:
        return self.label.split("T", 1)[1]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def current_slot(settings: Settings, at: datetime | None = None) -> Slot | None:
    """The scheduled slot whose window contains `at`, or None."""
    tz = ZoneInfo(settings.timezone)
    local = (at or now_utc()).astimezone(tz)
    if settings.end_date and local.date() > date.fromisoformat(settings.end_date):
        return None  # collection period is over
    for hour in settings.slots:
        start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if start <= local < start + timedelta(minutes=settings.slot_window_minutes):
            return Slot(f"{start:%Y-%m-%dT%H}", start.date(), True)
    return None


def manual_slot(settings: Settings, at: datetime | None = None) -> Slot:
    local = (at or now_utc()).astimezone(ZoneInfo(settings.timezone))
    return Slot(f"{local:%Y-%m-%dT%H%M}m", local.date(), False)


def parse_slot(label: str) -> Slot:
    d = date.fromisoformat(label[:10])
    return Slot(label, d, not label.endswith("m"))


def slot_dir(observations: Path, slot: Slot) -> Path:
    return observations / f"date={slot.local_date.isoformat()}"


def shop_done(observations: Path, slot: Slot, runner: str, shop: str) -> bool:
    return (slot_dir(observations, slot) / f"{slot.file_prefix}__{runner}__{shop}.parquet").exists()


def run_done(health_dir: Path, slot: Slot, runner: str) -> bool:
    """A run is done when its health manifest exists (written at the very end of a run)."""
    return (health_dir / f"{slot.label}__{runner}.json").exists()


def expected_slots(settings: Settings, until: datetime | None = None) -> list[str]:
    """All scheduled slot labels from start_date whose window has already closed."""
    tz = ZoneInfo(settings.timezone)
    until_local = (until or now_utc()).astimezone(tz)
    d = date.fromisoformat(settings.start_date)
    last = until_local.date()
    if settings.end_date:
        last = min(last, date.fromisoformat(settings.end_date))
    out: list[str] = []
    while d <= last:
        for hour in settings.slots:
            start = datetime(d.year, d.month, d.day, hour, tzinfo=tz)
            if start + timedelta(minutes=settings.slot_window_minutes) <= until_local:
                out.append(f"{start:%Y-%m-%dT%H}")
        d += timedelta(days=1)
    return out
