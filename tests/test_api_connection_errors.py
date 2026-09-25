import pytest
import requests
from unittest.mock import patch

from check_pve import CheckState


def test_sslerror_is_reported_as_certificate_issue(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006

    with patch(
        "check_pve.requests.get",
        side_effect=requests.exceptions.SSLError("certificate verify failed"),
    ):
        pve_instance.request("https://mock-endpoint")

    pve_instance.output.assert_called_with(
        CheckState.UNKNOWN, "Could not connect to PVE API: Certificate validation failed"
    )


def test_connectionerror_with_cert_msg_is_reported_as_certificate_issue(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006

    with patch(
        "check_pve.requests.get",
        side_effect=requests.exceptions.ConnectionError("certificate verify failed"),
    ):
        pve_instance.request("https://mock-endpoint")

    pve_instance.output.assert_called_with(
        CheckState.UNKNOWN, "Could not connect to PVE API: Certificate validation failed"
    )


def test_connectionerror_with_dns_msg_is_reported_as_resolve_issue(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006

    with patch(
        "check_pve.requests.get",
        side_effect=requests.exceptions.ConnectionError(
            "Failed to establish a new connection: [Errno -2] Name or service not known"
        ),
    ):
        pve_instance.request("https://mock-endpoint")

    pve_instance.output.assert_called_with(
        CheckState.UNKNOWN, "Could not connect to PVE API: Failed to resolve hostname"
    )


def test_connectionerror_with_other_msg_falls_back_to_message(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006

    with patch(
        "check_pve.requests.get",
        side_effect=requests.exceptions.ConnectionError("some other error"),
    ):
        pve_instance.request("https://mock-endpoint")

    pve_instance.output.assert_called_with(
        CheckState.UNKNOWN, "Could not connect to PVE API: some other error"
    )


def test_readtimeout_is_reported_as_unknown(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006

    with patch(
        "check_pve.requests.get",
        side_effect=requests.exceptions.ReadTimeout("read timed out"),
    ):
        pve_instance.request("https://mock-endpoint")

    pve_instance.output.assert_called_with(
        CheckState.UNKNOWN, "Could not fetch data from API: Read timeout"
    )


def test_other_request_exception_is_reported_as_unknown(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006

    with patch(
        "check_pve.requests.get",
        side_effect=requests.exceptions.TooManyRedirects("too many redirects"),
    ):
        pve_instance.request("https://mock-endpoint")

    pve_instance.output.assert_called_with(
        CheckState.UNKNOWN, "Could not fetch data from API: TooManyRedirects"
    )


def test_unauthorized_is_reported_as_invalid_credentials(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006
    response = requests.Response()
    response.status_code = 401

    with patch("check_pve.requests.get", return_value=response):
        pve_instance.request("https://mock-endpoint")

    pve_instance.output.assert_called_with(
        CheckState.UNKNOWN, "Could not fetch data from API: Invalid username or password"
    )


def test_unsupported_request_method_is_unknown(pve_instance):
    pve_instance.options.api_endpoint = "mock-endpoint"
    pve_instance.options.api_port = 8006
    pve_instance.output.side_effect = SystemExit

    with pytest.raises(SystemExit):
        pve_instance.request("https://mock-endpoint", method="put")

    pve_instance.output.assert_called_with(CheckState.UNKNOWN, "Unsupported request method: put")
