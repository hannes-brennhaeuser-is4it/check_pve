import shlex
from unittest.mock import patch

import pytest
import requests

from check_pve import CheckPVE, CheckState

CLI_ARGS = "-e endpoint -u monitoring@pve -m api-connection"


def test_api_connection_does_not_require_node(pve_instance: CheckPVE) -> None:
    args = pve_instance.parse_args(shlex.split(f"{CLI_ARGS} -t token=secret"))

    assert args.mode == "api-connection" and args.node is None


def test_api_connection_succeeds(pve_instance: CheckPVE) -> None:
    pve_instance.options.api_user = "monitoring@pve"

    with patch.object(CheckPVE, "request", return_value={"version": "9.0"}) as request:
        pve_instance.check_api_connection()

    assert request.call_args.args[0].endswith("/api2/json/version")
    assert pve_instance.check_result == CheckState.OK
    assert pve_instance.check_message == "Login to PVE API as 'monitoring@pve' succeeded"


def test_api_connection_mode_reports_failures_as_critical() -> None:
    """Connection failures are CRITICAL so the service can act as dependency parent."""
    argv = ["check_pve.py"] + shlex.split(f"{CLI_ARGS} -t token=secret")

    with patch("sys.argv", argv):
        pve = CheckPVE()

    assert pve.api_error_state == CheckState.CRITICAL

    with patch("check_pve.requests.get", side_effect=requests.exceptions.ConnectTimeout("timeout")):
        with pytest.raises(SystemExit) as exc:
            pve.check()

    assert exc.value.code == CheckState.CRITICAL.value


def test_api_connection_mode_reports_failed_login_as_critical(
    capsys: pytest.CaptureFixture,
) -> None:
    argv = ["check_pve.py"] + shlex.split(f"{CLI_ARGS} -p wrong")
    response = requests.Response()
    response.status_code = 401

    with patch("sys.argv", argv), patch("check_pve.requests.post", return_value=response):
        with pytest.raises(SystemExit) as exc:
            CheckPVE()

    assert exc.value.code == CheckState.CRITICAL.value
    assert capsys.readouterr().out == (
        "PVE CRITICAL: Could not fetch data from API: Invalid username or password\n"
    )


def test_other_modes_keep_unknown_for_api_failures() -> None:
    argv = ["check_pve.py", "-e", "endpoint", "-u", "user", "-t", "t=s", "-m", "cluster"]

    with patch("sys.argv", argv):
        pve = CheckPVE()

    assert pve.api_error_state == CheckState.UNKNOWN
