from __future__ import annotations

import pytest

from hdr.domain.actions import derive_duty_events
from hdr.domain.fixtures import load_fixture, snapshot_from_fixture
from hdr.domain.notice import SchoolInfo, apply_closure, apply_delay, parse_notice
from hdr.domain.timeutil import hm


def school_from(data: dict) -> SchoolInfo:
    s = data["school"]
    return SchoolInfo(id=s["id"], name=s["name"], doors_open=hm(s["doors_open"]), classes_begin=hm(s["classes_begin"]))


def baseline(variant: str | None = None):
    data = load_fixture(variant=variant)
    snap = snapshot_from_fixture(data)
    snap.events += derive_duty_events(snap)
    return data, snap


def delayed(variant: str | None = None):
    data, snap = baseline(variant)
    notice = parse_notice(data["notice"]["raw"], school_from(data))
    after, affected = apply_delay(snap, notice)
    return after, affected


def closed(variant: str | None = None):
    data, snap = baseline(variant)
    notice = parse_notice(data["other_notices"]["closure"]["raw"], school_from(data))
    return apply_closure(snap, notice)


@pytest.fixture
def demo_baseline():
    return baseline()[1]


@pytest.fixture
def demo_delayed():
    return delayed()[0]


@pytest.fixture
def demo_school():
    return school_from(load_fixture())
