import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from auto_ready.simulator import SimulatorWindow


def test_simulator_delay_and_manual_ready(qtbot):
    window = SimulatorWindow(evidence_source="test_fake")
    qtbot.addWidget(window)
    window.show()
    window.reset()
    window.delay_spin.setValue(0)
    window.start_round()
    assert window.state == "ready"
    assert window.ready_button.isVisible()

    window.reset()
    assert window.state == "reset"
    window.delay_spin.setValue(1)
    window.start_round()
    assert window.state == "waiting"
    assert not window.ready_button.isVisible()
    qtbot.waitUntil(lambda: window.state == "ready", timeout=1600)
    assert window.ready_button.isVisible()

    window.reset()
    window.manual_ready()
    assert window.state == "ready"


def test_simulator_duplicate_and_distractor(qtbot):
    events = []
    window = SimulatorWindow(
        observer=lambda event, **fields: events.append((event, fields)),
        evidence_source="test_fake",
    )
    qtbot.addWidget(window)
    window.show()
    window.reset()
    window.duplicate_check.setChecked(True)
    window.distractor_check.setChecked(True)
    window.manual_ready()

    assert window.ready_button.isVisible()
    assert window.duplicate_button.isVisible()
    assert window.distractor_button.isVisible()
    assert window.ready_button.text() == window.duplicate_button.text()
    assert window.ready_button.size() == window.duplicate_button.size()
    qtbot.mouseClick(window.distractor_button, Qt.MouseButton.LeftButton)
    assert window.state == "ready"
    assert [event for event, _ in events] == [
        "simulator_button_mouse_press",
        "simulator_button_mouse_release",
        "simulator_button_clicked",
    ]
    assert all(fields["source"] == "test_fake" for _, fields in events)
    assert all(fields["button_id"] == "distractor" for _, fields in events)


def test_simulator_success_and_no_success_scenarios(qtbot):
    window = SimulatorWindow(evidence_source="test_fake")
    qtbot.addWidget(window)
    window.show()
    window.reset()
    window.manual_ready()
    qtbot.mouseClick(window.ready_button, Qt.MouseButton.LeftButton)
    assert window.state == "success"
    assert not window.ready_button.isVisible()

    window.scenario_combo.setCurrentIndex(1)
    window.manual_ready()
    qtbot.mouseClick(window.ready_button, Qt.MouseButton.LeftButton)
    assert window.state == "no_success"
    assert window.ready_button.isVisible()


def test_simulator_expiry_hides_ready_button(qtbot):
    window = SimulatorWindow(evidence_source="test_fake")
    qtbot.addWidget(window)
    window.show()
    window.reset()
    window.scenario_combo.setCurrentIndex(2)
    window.expiry_spin.setValue(1)
    window.manual_ready()
    assert window.ready_button.isVisible()

    qtbot.waitUntil(lambda: window.state == "expired", timeout=1800)
    assert not window.ready_button.isVisible()
