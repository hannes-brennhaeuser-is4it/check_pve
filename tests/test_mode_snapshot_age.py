from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from check_pve import CheckPVE, CheckState

VMS = [
    {"vmid": 100, "name": "other-vm", "type": "qemu", "node": "pve"},
    {"vmid": 101, "name": "test-vm", "type": "qemu", "node": "pve"},
]


@pytest.mark.parametrize("idx", ["test-vm", 101])
def test_snapshot_age_only_checks_selected_vm(pve_instance: CheckPVE, idx) -> None:
    """Snapshots of other guests must not affect the result for a selected guest."""
    pve_instance.options = pve_instance.parse_args(
        ["-e", "endpoint", "-u", "user", "-p", "password", "-m", "snapshot-age"]
        + ["-w", "3600", "-c", "7200"]
    )
    now = int(datetime.now(timezone.utc).timestamp())
    requested = []

    def request(url: str, **kwargs: dict) -> list:
        requested.append(url)
        if url.endswith("cluster/resources"):
            return VMS
        if "/100/" in url:
            return [{"name": "old", "snaptime": now - 86400}]
        return [{"name": "fresh", "snaptime": now - 60}, {"name": "current"}]

    with patch.object(CheckPVE, "request", side_effect=request):
        pve_instance.check_snapshot_age(idx)

    assert not any("/100/" in url for url in requested)
    assert pve_instance.check_result == CheckState.OK
    assert pve_instance.check_message == f"Age of all 1 snapshots of '{idx}' is OK"


def test_snapshot_age_unknown_guest(pve_instance: CheckPVE) -> None:
    pve_instance.options.node = None

    with patch.object(CheckPVE, "request", return_value=VMS):
        pve_instance.check_snapshot_age("missing")

    assert pve_instance.check_result == CheckState.UNKNOWN
    assert pve_instance.check_message == "VM or LXC 'missing' not found"
