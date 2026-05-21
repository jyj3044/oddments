from __future__ import annotations

import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from flet_ui.arduino_status_window import (
    _parent_pid_from_argv,
    _parent_watchdog_iteration,
    _should_exit_for_parent,
)
from flet_ui.pages.page_arduino import _page_loop_open
from arduino.serial_bridge import (
    parse_arduino_runtime_status_line,
    runtime_status_to_dict,
    write_arduino_runtime_status_file,
)


class ArduinoRuntimeStatusParserTests(unittest.TestCase):
    def test_parses_short_key_runtime_status_json(self) -> None:
        status = parse_arduino_runtime_status_line(
            '{"v":1,"t":"ILLIUM","r":"running","n":{"gate":12345}}',
            received_at=10.0,
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.version, 1)
        self.assertEqual(status.macro_type, "ILLIUM")
        self.assertEqual(status.run_state, "running")
        self.assertEqual(status.next_ms, {"gate": 12345})
        self.assertEqual(status.received_at, 10.0)

    def test_ignores_non_json_serial_lines(self) -> None:
        self.assertIsNone(parse_arduino_runtime_status_line("PROCESS_START"))

    def test_rejects_unknown_run_state(self) -> None:
        self.assertIsNone(
            parse_arduino_runtime_status_line(
                '{"v":1,"t":"ILLIUM","r":"broken","n":{"gate":1000}}'
            )
        )

    def test_clamps_negative_next_values_to_zero(self) -> None:
        status = parse_arduino_runtime_status_line(
            '{"v":1,"t":"LUCID","r":"idle","n":{"gate":-3}}'
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.next_ms, {"gate": 0})

    def test_writes_status_snapshot_file_for_external_window(self) -> None:
        status = parse_arduino_runtime_status_line(
            '{"v":1,"t":"LEN","r":"paused","n":{"gate":9876}}',
            received_at=12.5,
        )
        assert status is not None

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "arduino_runtime_status.json"
            write_arduino_runtime_status_file(status, path=path)

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                (
                    '{"v":1,"t":"LEN","r":"paused","n":{"gate":9876},'
                    '"received_at":12.5,'
                    '"raw":"{\\"v\\":1,\\"t\\":\\"LEN\\",\\"r\\":\\"paused\\",'
                    '\\"n\\":{\\"gate\\":9876}}"}'
                ),
            )

    def test_serializes_status_for_status_window(self) -> None:
        status = parse_arduino_runtime_status_line(
            '{"v":1,"t":"ILLIUM","r":"running","n":{"gate":12345}}',
            received_at=10.0,
        )
        assert status is not None

        self.assertEqual(
            runtime_status_to_dict(status),
            {
                "v": 1,
                "t": "ILLIUM",
                "r": "running",
                "n": {"gate": 12345},
                "received_at": 10.0,
                "raw": '{"v":1,"t":"ILLIUM","r":"running","n":{"gate":12345}}',
            },
        )

    def test_parent_pid_is_read_from_status_window_args(self) -> None:
        self.assertEqual(
            _parent_pid_from_argv(["prog", "--parent-pid", "1234"]),
            1234,
        )
        self.assertEqual(
            _parent_pid_from_argv(["prog", "--parent-pid", "bad"]),
            0,
        )
        self.assertEqual(_parent_pid_from_argv(["prog"]), 0)

    def test_current_process_is_detected_alive(self) -> None:
        from flet_ui.arduino_status_window import _process_alive

        self.assertTrue(_process_alive(os.getpid()))
        self.assertFalse(_process_alive(0))

    def test_status_window_exits_only_when_tracked_parent_is_gone(self) -> None:
        self.assertFalse(_should_exit_for_parent(0))
        self.assertFalse(_should_exit_for_parent(os.getpid()))
        self.assertTrue(_should_exit_for_parent(99999999))

    def test_parent_watchdog_iteration_invokes_exit_when_parent_is_gone(self) -> None:
        called: list[bool] = []

        _parent_watchdog_iteration(99999999, exit_func=lambda: called.append(True))

        self.assertEqual(called, [True])

    def test_page_loop_open_rejects_closed_loop(self) -> None:
        class FakeLoop:
            def is_closed(self) -> bool:
                return True

        class FakeConnection:
            loop = FakeLoop()

        class FakeSession:
            connection = FakeConnection()

        class FakePage:
            session = FakeSession()

        self.assertFalse(_page_loop_open(FakePage()))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
