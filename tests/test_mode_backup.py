from unittest.mock import patch

from check_pve import CheckPVE, CheckState, RequestError


def test_unreadable_pool_is_reported_after_status_line(pve_instance: CheckPVE, capsys) -> None:
    """Pool lookup failures are appended to the message instead of printed first."""
    pve_instance.options = pve_instance.parse_args(
        ["-e", "endpoint", "-u", "user", "-p", "password", "-m", "backup", "--ignore-pools", "p1"]
    )
    pve_instance.check_result = CheckState.OK

    def request(url: str, **kwargs: dict) -> list:
        if url.endswith("pools/p1"):
            raise RequestError("denied", 403)
        if url.endswith("not-backed-up"):
            return [{"vmid": 100}]
        return []

    with patch.object(CheckPVE, "request", side_effect=request):
        pve_instance.check_vzdump_backup()

    assert capsys.readouterr().out == ""
    assert pve_instance.check_result == CheckState.WARNING
    assert pve_instance.check_message.splitlines()[0].startswith("0 backup tasks successful")
    assert "Unable to fetch members of pool(s) 'p1'" in pve_instance.check_message
