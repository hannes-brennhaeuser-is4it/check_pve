from unittest.mock import patch

import pytest

import check_pve
from check_pve import CheckPVE, CheckState


def test_unexpected_exception_exits_unknown(capsys: pytest.CaptureFixture) -> None:
    """Uncaught exceptions must not end with Python's default exit code 1 (WARNING)."""
    with patch.object(CheckPVE, "__init__", side_effect=KeyError("status")):
        with pytest.raises(SystemExit) as exc:
            check_pve.main()

    assert exc.value.code == CheckState.UNKNOWN.value
    assert capsys.readouterr().out == "UNKNOWN - Unexpected error: KeyError\n"


def test_system_exit_is_not_intercepted() -> None:
    """Regular plugin exits must pass through the global handler unchanged."""
    with patch.object(CheckPVE, "__init__", side_effect=SystemExit(CheckState.CRITICAL.value)):
        with pytest.raises(SystemExit) as exc:
            check_pve.main()

    assert exc.value.code == CheckState.CRITICAL.value


def test_unreadable_credentials_file_exits_unknown(pve_instance: CheckPVE, tmp_path) -> None:
    pve_instance.get_file_line(str(tmp_path / "missing"))

    pve_instance.output.assert_called_with(CheckState.UNKNOWN, "Could not read credentials file")
