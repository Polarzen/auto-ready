"""Native evidence tests use only a fake SendInput API, never the Windows desktop."""

import threading

import pytest
from test_native import FakeInputAPI

from auto_ready.native import SafetyError, WindowsInput


@pytest.mark.parametrize("result", [0, 1, 2, 3])
def test_system_result_records_actual_return_without_claiming_receipt(result):
    api = FakeInputAPI(result=result)
    recorded = []

    def observe(event, **fields):
        recorded.append((event, fields))
        # A partial down must be released before potentially slow evidence I/O.
        if result == 2:
            assert len(api.calls) == 2

    adapter = WindowsInput(_api=api, observer=observe)
    if result == 3:
        adapter.click((0, 0), threading.Event())
    else:
        with pytest.raises(SafetyError):
            adapter.click((0, 0), threading.Event())
    batch = [f for e, f in recorded if not f["cleanup"]]
    assert len(batch) == 1 and batch[0]["sent"] == result
    assert batch[0]["requested"] == 3
    assert not any("received" in f or "success" in f for _, f in recorded)
    assert sum(f["cleanup"] for _, f in recorded) == int(result == 2)


def test_unknown_system_result_records_unknown_and_releases_before_observer():
    api = FakeInputAPI(failure=OSError("native fake failure"))
    records = []

    def observe(event, **fields):
        assert len(api.calls) == 2
        records.append(fields)

    with pytest.raises(OSError):
        WindowsInput(_api=api, observer=observe).click((0, 0), threading.Event())
    assert next(f for f in records if not f["cleanup"])["sent"] is None


def test_observer_failure_does_not_retry_or_skip_release():
    api = FakeInputAPI(result=2)

    def broken_observer(*args, **kwargs):
        raise OSError("storage failure")

    with pytest.raises(SafetyError):
        WindowsInput(_api=api, observer=broken_observer).click((0, 0), threading.Event())
    assert [call[0] for call in api.calls] == [3, 1]
