import shlex

import pytest

from check_pve import CheckPVE, CheckState

CLI_ARGS = "-e endpoint -u user -p password"


@pytest.mark.parametrize(
    "cli_args, error",
    [
        pytest.param(
            "-m cluster", "The following arguments are required: --api-endpoint", id="missing"
        ),
        pytest.param(f"{CLI_ARGS} -m invalid", "argument -m/--mode: invalid choice", id="choice"),
        pytest.param(
            f"{CLI_ARGS} -m cpu -n pve -w a:b", "Invalid threshold format", id="threshold"
        ),
        pytest.param(
            f"{CLI_ARGS} -m cpu -n pve -w 90 -c 80",
            "Critical value must be greater than warning value",
            id="threshold-order",
        ),
        pytest.param(f"{CLI_ARGS} -m cpu", "--mode cpu requires node name (--node)", id="node"),
        pytest.param(
            f"{CLI_ARGS} -m vm", "--mode vm requires either vm name (--name) or id", id="vm"
        ),
        pytest.param(
            f"{CLI_ARGS} -m storage -n pve", "--mode storage requires storage name", id="storage"
        ),
    ],
)
def test_argument_error_exits_unknown(
    pve_instance: CheckPVE, capsys: pytest.CaptureFixture, cli_args: str, error: str
) -> None:
    """Argument errors are reported as UNKNOWN with the status line first."""
    with pytest.raises(SystemExit) as exc:
        pve_instance.parse_args(shlex.split(cli_args))

    assert exc.value.code == CheckState.UNKNOWN.value
    status, usage = capsys.readouterr().out.splitlines()[:2]
    assert status.startswith("PVE UNKNOWN: ")
    assert error in status
    assert usage.startswith("usage: ")


@pytest.mark.parametrize("mode", ["vm", "vm_status", "vm-status"])
def test_vm_modes_do_not_require_node(pve_instance: CheckPVE, mode: str) -> None:
    args = pve_instance.parse_args(shlex.split(f"{CLI_ARGS} -m {mode} --vmid 100"))

    assert args.mode == mode and args.node is None
