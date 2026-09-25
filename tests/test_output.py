from unittest.mock import patch

import pytest

from check_pve import CheckPVE, CheckState


def test_check_output_layout(pve_instance: CheckPVE, capsys: pytest.CaptureFixture) -> None:
    """Summary first, non-OK details below with state suffix, perfdata last."""
    pve_instance.output = CheckPVE.output
    pve_instance.check_result = CheckState.CRITICAL
    pve_instance.check_message = "1 of 3 items failed"
    pve_instance.add_detail("item a", CheckState.OK)
    pve_instance.add_detail("item b", CheckState.CRITICAL)
    pve_instance.add_detail("item c", CheckState.WARNING)
    pve_instance.add_detail("note without state")
    pve_instance.perfdata = ["m=1;;;0;"]

    with pytest.raises(SystemExit) as exc:
        pve_instance.check_output()

    assert exc.value.code == CheckState.CRITICAL.value
    assert capsys.readouterr().out.splitlines() == [
        "PVE CRITICAL: 1 of 3 items failed",
        "item b [CRITICAL]",
        "item c [WARNING]",
        "note without state|m=1;;;0;",
    ]


def test_services_summary(pve_instance: CheckPVE) -> None:
    pve_instance.options.ignore_services = ["ignored"]
    services = [
        {"name": "pveproxy", "desc": "PVE API Proxy", "state": "running"},
        {"name": "corosync", "desc": "Corosync", "state": "dead", "active-state": "active"},
        {"name": "optional", "desc": "Optional", "state": "dead", "active-state": "inactive"},
        {"name": "ignored", "desc": "Ignored", "state": "dead", "active-state": "active"},
    ]

    with patch.object(CheckPVE, "request", return_value=services):
        pve_instance.check_services()

    assert pve_instance.check_result == CheckState.CRITICAL
    assert pve_instance.check_message == "1 of 2 services are not running"
    assert pve_instance.get_details() == "\nCorosync (corosync) is not running [CRITICAL]"


def test_zfs_health_summary(pve_instance: CheckPVE) -> None:
    pools = [{"name": "rpool", "health": "ONLINE"}, {"name": "tank", "health": "DEGRADED"}]

    with patch.object(CheckPVE, "request", return_value=pools):
        pve_instance.check_zfs_health()

    assert pve_instance.check_result == CheckState.CRITICAL
    assert pve_instance.check_message == "1 of 2 ZFS pools are not healthy"
    assert pve_instance.get_details() == "\ntank: DEGRADED [CRITICAL]"


def test_replication_summary(pve_instance: CheckPVE) -> None:
    pve_instance.options.vmid = None
    pve_instance.options.node = "pve"
    pve_instance.options.threshold_warning = {}
    pve_instance.options.threshold_critical = {}
    jobs = [
        {"id": "100-0", "guest": 100, "fail_count": 0, "duration": 2.5},
        {"id": "101-0", "guest": 101, "fail_count": 3, "error": "timeout"},
    ]

    with patch.object(CheckPVE, "request", return_value=jobs):
        pve_instance.check_replication()

    assert pve_instance.check_result == CheckState.WARNING
    assert pve_instance.check_message == "1 of 2 replication jobs failed on node 'pve'"
    assert pve_instance.get_details() == (
        "\nGuest 101 (job 101-0): 3 failures, error: timeout [WARNING]"
    )
    assert pve_instance.perfdata == ["duration_100-0=2.5s;;;0;"]


def test_detail_option_lists_ok_items(pve_instance: CheckPVE) -> None:
    pve_instance.options = pve_instance.parse_args(
        ["-e", "endpoint", "-u", "user", "-p", "password", "-m", "zfs-health", "-n", "pve"]
        + ["--detail"]
    )
    pools = [{"name": "rpool", "health": "ONLINE"}, {"name": "tank", "health": "DEGRADED"}]

    with patch.object(CheckPVE, "request", return_value=pools):
        pve_instance.check_zfs_health()

    assert pve_instance.get_details() == "\nrpool: ONLINE [OK]\ntank: DEGRADED [CRITICAL]"
