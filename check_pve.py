#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ------------------------------------------------------------------------------
# check_pve.py - A check plugin for Proxmox Virtual Environment (PVE).
# Copyright (C) 2018-2026  Nicolai Buchwitz <nb@tipi-net.de>
#
# Version: 1.6.0+is4it.1.3.5
#
# ------------------------------------------------------------------------------
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
# ------------------------------------------------------------------------------

"""Proxmox VE monitoring check command for various monitoring systems like Icinga and others."""

import re
import sys
from typing import Callable, Dict, NoReturn, Optional, Union, List

try:
    import argparse
    from datetime import datetime, timezone
    from enum import Enum

    import requests
    from packaging import version
    from requests.packages.urllib3.exceptions import InsecureRequestWarning

except ImportError as e:
    # CheckState is not defined yet, 3 is UNKNOWN
    print(f"PVE UNKNOWN: Missing python module: {str(e)}")
    sys.exit(3)

# Timeout for API requests in seconds
CHECK_API_TIMEOUT = 30


def compare_thresholds(
    threshold_warning: Dict, threshold_critical: Dict, comparator: Callable
) -> bool:
    """Perform sanity checks on thresholds parameters (used for argparse validation)."""
    ok = True
    keys = set(list(threshold_warning.keys()) + list(threshold_critical.keys()))
    for key in keys:
        if (key in threshold_warning and key in threshold_critical) or (
            None in threshold_warning and None in threshold_critical
        ):
            ok = ok and comparator(threshold_warning[key], threshold_critical[key])
        elif key in threshold_warning and None in threshold_critical:
            ok = ok and comparator(threshold_warning[key], threshold_critical[None])
        elif key in threshold_critical and None in threshold_warning:
            ok = ok and comparator(threshold_warning[None], threshold_critical[key])

    return ok


class CheckState(Enum):
    """Check return values."""

    OK = 0
    WARNING = 1
    CRITICAL = 2
    UNKNOWN = 3


class CheckThreshold:
    """Threshold representation used by the check command."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __eq__(self, other: "CheckThreshold") -> bool:
        """Threshold is equal to given one."""
        return self.value == other.value

    def __lt__(self, other: "CheckThreshold") -> bool:
        """Threshold is lower to given one."""
        return self.value < other.value

    def __le__(self, other: "CheckThreshold") -> bool:
        """Threshold is lower or equal to given one."""
        return self.value <= other.value

    def __gt__(self, other: "CheckThreshold") -> bool:
        """Threshold is greater than given one."""
        return self.value > other.value

    def __ge__(self, other: "CheckThreshold") -> bool:
        """Threshold is greater or equal than given one."""
        return self.value >= other.value

    def check(self, value: float, lower: bool = False) -> bool:
        """Check threshold value as upper or lower boundary for given value."""
        if lower:
            return value < self.value

        return value > self.value

    @staticmethod
    def threshold_type(arg: str) -> Dict[str, "CheckThreshold"]:
        """Convert string argument(s) to threshold dict."""
        thresholds = {}

        try:
            thresholds[None] = CheckThreshold(float(arg))
        except ValueError:
            for t in arg.split(","):
                m = re.match("([a-z_0-9]+):([0-9.]+)", t)

                if m:
                    thresholds[m.group(1)] = CheckThreshold(float(m.group(2)))
                else:
                    raise argparse.ArgumentTypeError(f"Invalid threshold format: {t}")  # noqa: B904

        return thresholds


class CheckArgumentParser(argparse.ArgumentParser):
    """Argument parser reporting usage errors as UNKNOWN check result."""

    def error(self, message: str) -> NoReturn:
        """Print status line followed by usage and exit with UNKNOWN."""
        CheckPVE.output(
            CheckState.UNKNOWN, f"{self.prog}: error: {message}\n{self.format_usage().rstrip()}"
        )


class RequestError(Exception):
    """Exception for request related errors."""

    def __init__(self, message: str, rc: int) -> None:
        self.message = message
        self.rc = rc

        super().__init__(self.message)


class CheckPVE:
    """Check command for Proxmox VE."""

    SHORTNAME = "PVE"
    VERSION = "1.6.0+is4it.1.3.5"
    API_URL = "https://{hostname}:{port}/api2/json/{command}"
    UNIT_SCALE = {
        "GB": 10**9,
        "MB": 10**6,
        "KB": 10**3,
        "GiB": 2**30,
        "MiB": 2**20,
        "KiB": 2**10,
        "B": 1,
    }

    def check_output(self) -> None:
        """Print check command output with perfdata and return code."""
        message = self.check_message + self.get_details()
        if self.perfdata:
            message += self.get_perfdata()

        self.output(self.check_result, message)

    def add_detail(self, text: str, state: Optional[CheckState] = None) -> None:
        """Add a long output line; lines without state are always shown."""
        self.details.append((state, text))

    def get_details(self) -> str:
        """Get long output lines, one per line; OK lines only with --detail."""
        lines = [
            f"{text} [{state.name}]" if state else text
            for state, text in self.details
            if state is not CheckState.OK or self.options.detail
        ]
        return "".join(f"\n{line}" for line in lines)

    @staticmethod
    def output(rc: CheckState, message: str) -> None:
        """Print message to stdout and exit with given return code."""
        print(f"{CheckPVE.SHORTNAME} {rc.name}: {message}")
        sys.exit(rc.value)

    def get_url(self, command: str) -> str:
        """Get API url for specific command."""
        return self.API_URL.format(
            hostname=self.options.api_endpoint, command=command, port=self.options.api_port
        )

    def get_file_line(self, filename: str) -> str:
        """Read the first line of a file and return it without the newline."""
        try:
            with open(filename, "r") as file:
                return file.readline().strip()
        except OSError:
            self.output(CheckState.UNKNOWN, "Could not read credentials file")

    def request(self, url: str, method: str = "get", **kwargs: Dict) -> Union[Dict, None]:
        """Execute request against Proxmox VE API and return json data."""
        response = None
        try:
            if method == "post":
                response = requests.post(
                    url,
                    verify=not self.options.api_insecure,
                    data=kwargs.get("data", None),
                    timeout=5,
                )
            elif method == "get":
                response = requests.get(
                    url,
                    verify=not self.options.api_insecure,
                    cookies=self.__cookies,
                    headers=self.__headers,
                    params=kwargs.get("params", None),
                    timeout=CHECK_API_TIMEOUT,
                )
            else:
                self.output(CheckState.CRITICAL, f"Unsupport request method: {method}")
        except requests.exceptions.ConnectTimeout:
            self.output(CheckState.UNKNOWN, "Could not connect to PVE API: Connection timeout")
            return None
        except requests.exceptions.SSLError:
            self.output(
                CheckState.UNKNOWN, "Could not connect to PVE API: Certificate validation failed"
            )
            return None
        except requests.exceptions.ConnectionError as e:
            # Older Python / requests combinations may wrap SSL certificate
            # validation failures in ConnectionError instead of SSLError; inspect
            # the exception text to determine the real reason.
            msg = str(e).lower()
            if "certificate verify failed" in msg or "ssl" in msg and "certificate" in msg:
                self.output(
                    CheckState.UNKNOWN,
                    "Could not connect to PVE API: Certificate validation failed",
                )
            elif (
                "name or service not known" in msg
                or "failed to establish a new connection" in msg
                or "failed to resolve" in msg
                or "gaierror" in msg
            ):
                self.output(
                    CheckState.UNKNOWN, "Could not connect to PVE API: Failed to resolve hostname"
                )
            else:
                # Fallback to showing the underlying exception message for clarity
                self.output(CheckState.UNKNOWN, f"Could not connect to PVE API: {str(e)}")
            return None
        except requests.exceptions.Timeout:
            self.output(CheckState.UNKNOWN, "Could not fetch data from API: Read timeout")
            return None
        except requests.exceptions.RequestException as e:
            self.output(CheckState.UNKNOWN, f"Could not fetch data from API: {type(e).__name__}")
            return None

        if response.ok:
            return response.json()["data"]

        message = "Could not fetch data from API: "
        if response.status_code == 401:
            message += "Invalid username or password"
        elif response.status_code == 403:
            message += (
                "Access denied. Please check if API user has sufficient permissions / "
                "the correct role has been assigned."
            )
        else:
            message += f"HTTP error code was {response.status_code}"

        if kwargs.get("raise_error", False):
            raise RequestError(message, response.status_code)

        self.output(CheckState.UNKNOWN, message)

    def get_ticket(self) -> str:
        """Perform login and fetch ticket for further API calls."""
        url = self.get_url("access/ticket")
        data = {"username": self.options.api_user, "password": self.options.api_password}
        result = self.request(url, "post", data=data)

        return result["ticket"]

    def check_api_value(self, url: StopIteration, message: str, **kwargs: Dict) -> None:
        """Perform simple threshold based check command."""
        result = self.request(url)
        used = None

        if "key" in kwargs:
            result = result[kwargs.get("key")]

        if isinstance(result, (dict,)):
            used_percent = self.get_value(result["used"], result["total"])
            used = self.get_value(result["used"])
            total = self.get_value(result["total"])

            self.add_perfdata(kwargs.get("perfkey", "usage"), used_percent)
            self.add_perfdata(
                kwargs.get("perfkey", "used"), used, max=total, unit=self.options.unit
            )
        else:
            used_percent = round(float(result) * 100, 2)
            self.add_perfdata(kwargs.get("perfkey", "usage"), used_percent)

        if self.options.values_mb:
            message += f" {used} {self.options.unit}"
            value = used
        else:
            message += f" {used_percent} %"
            value = used_percent

        self.check_thresholds(value, message)

    def check_vm_status(self, idx: Union[str, int], **kwargs: str) -> None:
        """Check status of virtual machine by vmid or name."""
        url = self.get_url(
            "cluster/resources",
        )
        data = self.request(url, params={"type": "vm"})

        expected_state = kwargs.get("expected_state", "running")
        only_status = kwargs.get("only_status", False)

        found = False
        for vm in data:
            if idx in (vm.get("name", None), vm.get("vmid", None)):
                # Check if VM (default) or LXC
                vm_type = "VM"
                if vm["type"] == "lxc":
                    vm_type = "LXC"

                if vm["status"] != expected_state:
                    self.check_message = (
                        f"{vm_type} '{vm['name']}' is {vm['status']} (expected: {expected_state})"
                    )
                    if not self.options.ignore_vm_status:
                        self.check_result = CheckState.CRITICAL
                else:
                    if self.options.node and self.options.node != vm["node"]:
                        self.check_message = (
                            f"{vm_type} '{vm['name']}' is {expected_state}, "
                            f"but located on node '{vm['node']}' instead of '{self.options.node}'"
                        )
                        self.check_result = CheckState.WARNING
                    else:
                        self.check_message = (
                            f"{vm_type} '{vm['name']}' is {expected_state} on node '{vm['node']}'"
                        )

                if vm["status"] == "running" and not only_status:
                    cpu = round(vm["cpu"] * 100, 2)
                    self.add_perfdata("cpu", cpu)

                    if self.options.values_mb:
                        memory = self.scale_value(vm["mem"])
                        self.add_perfdata(
                            "memory",
                            memory,
                            unit=self.options.unit,
                            max=self.scale_value(vm["maxmem"]),
                        )
                        disk = self.scale_value(vm["disk"])
                        self.add_perfdata(
                            "disk",
                            disk,
                            unit=self.options.unit,
                            max=self.scale_value(vm["maxdisk"]),
                        )

                    else:
                        memory = self.get_value(vm["mem"], vm["maxmem"])
                        self.add_perfdata("memory", memory)
                        disk = self.get_value(vm["disk"], vm["maxdisk"])
                        self.add_perfdata("disk", disk)

                    self.check_thresholds(
                        {"cpu": cpu, "memory": memory, "disk": disk}, message=self.check_message
                    )

                found = True
                break

        if not found:
            self.check_message = f"VM or LXC '{idx}' not found"
            self.check_result = CheckState.WARNING

    def check_disks(self) -> None:
        """Check disk health on specific Proxmox VE node.

        Disks can be excluded from the check by device name or serial number.
        """
        url = self.get_url(f"nodes/{self.options.node}/disks")

        ignore_disks = {entry.strip().lower() for entry in self.options.ignore_disks}

        checked = 0
        failed = 0
        unknown = 0
        disks = self.request(url + "/list")
        for disk in disks:
            name = disk["devpath"].replace("/dev/", "")
            serial = disk.get("serial", "") or ""

            if name.lower() in ignore_disks or (serial and serial.lower() in ignore_disks):
                continue

            checked += 1
            detail = f"{disk['devpath']} with serial '{serial}': health {disk['health']}"
            if disk["health"] == "UNKNOWN":
                unknown += 1
                self.add_detail(detail, CheckState.WARNING)
            elif disk["health"] not in ("PASSED", "OK"):
                failed += 1
                self.add_detail(detail, CheckState.WARNING)
            else:
                self.add_detail(detail, CheckState.OK)

            if disk["wearout"] != "N/A":
                self.add_perfdata(f"wearout_{name}", disk["wearout"])

        if failed or unknown:
            self.check_result = CheckState.WARNING
            problems = []
            if failed:
                problems.append(f"{failed} of {checked} disks failed the health test")
            if unknown:
                problems.append(f"{unknown} of {checked} disks have unknown health status")
            self.check_message = ", ".join(problems)
        else:
            self.check_message = f"All {checked} disks are healthy"

    def check_replication(self) -> None:
        """Check replication status for either all or one specific vm / container."""
        url = self.get_url(f"nodes/{self.options.node}/replication")

        if self.options.vmid:
            data = self.request(url, params={"guest": self.options.vmid})
        else:
            data = self.request(url)

        failed = 0
        for job in data:
            detail = f"Guest {job['guest']} (job {job['id']})"
            if job["fail_count"] > 0:
                failed += 1
                self.add_detail(
                    f"{detail}: {job['fail_count']} failures, error: {job['error']}",
                    CheckState.WARNING,
                )
            else:
                self.add_detail(f"{detail}: last run took {job['duration']}s", CheckState.OK)
                self.add_perfdata("duration_" + job["id"], job["duration"], unit="s")

        if failed:
            self.check_result = CheckState.WARNING
            self.check_message = (
                f"{failed} of {len(data)} replication jobs failed on node '{self.options.node}'"
            )
        elif data:
            self.check_result = CheckState.OK
            self.check_message = (
                f"All {len(data)} replication jobs on node '{self.options.node}' are OK"
            )
        else:
            self.check_result = CheckState.OK
            self.check_message = f"No replication jobs on node '{self.options.node}'"

    def check_services(self) -> None:
        """Check state of core services on Proxmox VE node."""
        url = self.get_url(f"nodes/{self.options.node}/services")
        data = self.request(url)

        checked = 0
        failed = 0
        for service in data:
            if service["name"] in self.options.ignore_services:
                continue

            detail = f"{service['desc']} ({service['name']})"
            if service["state"] == "running":
                checked += 1
                self.add_detail(f"{detail} is running", CheckState.OK)
            elif service.get("active-state", "active") == "active":
                checked += 1
                failed += 1
                self.add_detail(f"{detail} is not running", CheckState.CRITICAL)

        if failed:
            self.check_result = CheckState.CRITICAL
            self.check_message = f"{failed} of {checked} services are not running"
        else:
            self.check_message = f"All {checked} services are running"

    def check_subscription(self) -> None:
        """Check subscription status on Proxmox VE node."""
        url = self.get_url(f"nodes/{self.options.node}/subscription")
        data = self.request(url)

        # 'status' is an enum, values are documented in Proxmox's API viewer:
        # https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/subscription
        if data["status"].lower() == "new":
            self.check_result = CheckState.WARNING
            self.check_message = "Subscription not yet checked"
        elif data["status"].lower() == "notfound":
            self.check_result = CheckState.WARNING
            self.check_message = "No valid subscription found"
        elif data["status"].lower() == "suspended":
            self.check_result = CheckState.WARNING
            self.check_message = "Subscription suspended"
        elif data["status"].lower() == "expired":
            self.check_result = CheckState.CRITICAL
            self.check_message = "Subscription expired"
        elif data["status"].lower() == "invalid":
            self.check_result = CheckState.CRITICAL
            self.check_message = "Subscription invalid"
        elif data["status"].lower() == "active":
            subscription_due_date = data["nextduedate"]
            subscription_product_name = data["productname"]

            date_expire = datetime.strptime(subscription_due_date, "%Y-%m-%d")
            date_today = datetime.today()
            delta = (date_expire - date_today).days

            message = f"{subscription_product_name} is valid until {subscription_due_date}"
            message_warning_critical = (
                f"{subscription_product_name} will expire in {delta} days ({subscription_due_date})"
            )

            self.check_thresholds(
                delta,
                message,
                messageWarning=message_warning_critical,
                messageCritical=message_warning_critical,
                lowerValue=True,
            )
        else:
            self.check_result = CheckState.UNKNOWN
            self.check_message = "PVE API returned unexpected status '{}'".format(data["status"])

    def check_updates(self) -> None:
        """Check for package updates on Proxmox VE node."""
        url = self.get_url(f"nodes/{self.options.node}/apt/update")
        count = len(self.request(url))

        if count:
            self.check_result = CheckState.WARNING
            msg = "{} pending update"
            if count > 1:
                msg += "s"
            self.check_message = msg.format(count)
        else:
            self.check_message = "System up to date"

    def check_cluster_status(self) -> None:
        """Check if cluster is operational."""
        url = self.get_url("cluster/status")
        data = self.request(url)

        nodes = {}
        quorate = None
        cluster = ""
        for elem in data:
            if elem["type"] == "cluster":
                quorate = elem["quorate"]
                cluster = elem["name"]
            elif elem["type"] == "node":
                nodes[elem["name"]] = elem["online"]

        for node, online in nodes.items():
            if online:
                self.add_detail(f"Node '{node}' is online", CheckState.OK)
            else:
                self.add_detail(f"Node '{node}' is offline", CheckState.WARNING)

        if quorate is None:
            self.check_message = "No cluster configuration found"
        elif quorate:
            node_count = len(nodes)
            nodes_online_count = len({k: v for k, v in nodes.items() if v})

            if node_count > nodes_online_count:
                diff = node_count - nodes_online_count
                self.check_result = CheckState.WARNING
                self.check_message = (
                    f"Cluster '{cluster}' is healthy, but {diff} of {node_count} nodes offline"
                )
            else:
                self.check_result = CheckState.OK
                self.check_message = (
                    f"Cluster '{cluster}' is healthy, all {node_count} nodes online"
                )

            self.add_perfdata("nodes_total", node_count, unit="")
            self.add_perfdata("nodes_online", nodes_online_count, unit="")
        else:
            self.check_result = CheckState.CRITICAL
            self.check_message = "Cluster is unhealthy - no quorum"

    def check_zfs_fragmentation(self, name: Optional[str] = None) -> None:
        """Check all or one specific ZFS pool for fragmentation."""
        url = self.get_url(f"nodes/{self.options.node}/disks/zfs")
        data = self.request(url)

        warnings = []
        critical = []
        found = name is None
        for pool in data:
            found = found or name == pool["name"]
            if (name is not None and name == pool["name"]) or name is None:
                key = "fragmentation"
                if name is None:
                    key += f"_{pool['name']}"
                self.add_perfdata(key, pool["frag"])

                threshold_name = f"fragmentation_{name}"
                threshold_warning = self.threshold_warning(threshold_name)
                threshold_critical = self.threshold_critical(threshold_name)

                state = CheckState.OK
                if threshold_critical is not None and pool["frag"] > float(
                    threshold_critical.value
                ):
                    critical.append(pool)
                    state = CheckState.CRITICAL
                elif threshold_warning is not None and pool["frag"] > float(
                    threshold_warning.value
                ):
                    warnings.append(pool)
                    state = CheckState.WARNING

                if name is None:
                    self.add_detail(f"{pool['name']}: {pool['frag']} %", state)

        if not found:
            self.check_result = CheckState.UNKNOWN
            self.check_message = f"Could not fetch fragmentation of ZFS pool '{name}'"
        else:
            if warnings or critical:
                value = None
                if critical:
                    self.check_result = CheckState.CRITICAL
                    if name is not None:
                        value = critical[0]["frag"]
                else:
                    self.check_result = CheckState.WARNING
                    if name is not None:
                        value = warnings[0]["frag"]

                if name is not None:
                    self.check_message = (
                        f"Fragmentation of ZFS pool '{name}' is above thresholds: {value} %"
                    )
                else:
                    pool_above = len(warnings) + len(critical)
                    self.check_message = (
                        f"{pool_above} of {len(data)} ZFS pools are above fragmentation thresholds"
                    )
            else:
                self.check_result = CheckState.OK
                if name is not None:
                    self.check_message = f"Fragmentation of ZFS pool '{name}' is OK"
                else:
                    self.check_message = f"Fragmentation of all {len(data)} ZFS pools is OK"

    def check_zfs_health(self, name: Optional[str] = None) -> None:
        """Check all or one specific ZFS pool for health."""
        url = self.get_url(f"nodes/{self.options.node}/disks/zfs")
        data = self.request(url)

        unhealthy = []
        checked = 0
        found = name is None
        healthy_conditions = ["online"]
        for pool in data:
            found = found or name == pool["name"]
            if (name is not None and name == pool["name"]) or name is None:
                checked += 1
                state = CheckState.OK
                if pool["health"].lower() not in healthy_conditions:
                    unhealthy.append(pool)
                    state = CheckState.CRITICAL
                if name is None:
                    self.add_detail(f"{pool['name']}: {pool['health']}", state)

        if not found:
            self.check_result = CheckState.UNKNOWN
            self.check_message = f"Could not fetch health of ZFS pool '{name}'"
        else:
            if unhealthy:
                self.check_result = CheckState.CRITICAL
                if name is not None:
                    self.check_message = (
                        f"ZFS pool '{name}' is not healthy: {unhealthy[0]['health']}"
                    )
                else:
                    self.check_message = f"{len(unhealthy)} of {checked} ZFS pools are not healthy"
            else:
                self.check_result = CheckState.OK
                if name is not None:
                    self.check_message = f"ZFS pool '{name}' is healthy"
                else:
                    self.check_message = f"All {checked} ZFS pools are healthy"

    def check_ceph_health(self) -> None:
        """Check health of CEPH cluster."""
        url = self.get_url("cluster/ceph/status")
        data = self.request(url)
        ceph_health = data.get("health", {})

        if "status" not in ceph_health:
            self.check_result = CheckState.UNKNOWN
            self.check_message = (
                "Could not fetch Ceph status from API. "
                "Check the output of 'pvesh get cluster/ceph' on your node"
            )
            return

        if ceph_health["status"] == "HEALTH_OK":
            self.check_result = CheckState.OK
            self.check_message = "Ceph Cluster is healthy"
        elif ceph_health["status"] == "HEALTH_WARN":
            self.check_result = CheckState.WARNING
            self.check_message = "Ceph Cluster is in warning state"
        elif ceph_health["status"] == "HEALTH_CRIT":
            self.check_result = CheckState.CRITICAL
            self.check_message = "Ceph Cluster is in critical state"
        else:
            self.check_result = CheckState.UNKNOWN
            self.check_message = "Ceph Cluster is in unknown state"

    def check_network_status(self, name: Optional[str] = None) -> None:
        """Check network interface status and bond health."""
        url = self.get_url(f"nodes/{self.options.node}/network")
        data = self.request(url)

        if not data:
            self.check_result = CheckState.UNKNOWN
            self.check_message = "Could not fetch network interface data from API"
            return

        interfaces_down = []
        bonds_degraded = []
        entries = []  # format: [(state, text)] for each checked interface
        bond_count = 0
        found = name is None

        for iface in data:
            iface_name = iface.get("iface", "")
            iface_type = iface.get("type", "")

            # Skip ignored interfaces
            if iface_name in self.options.ignore_interfaces:
                continue

            # Check if this is the interface we're looking for (if name specified)
            if name is not None:
                if iface_name == name:
                    found = True
                else:
                    # Skip if we're looking for a specific interface and this isn't it
                    continue

            # Check if interface is active
            is_active = iface.get("active", 0)

            # Check bond status
            if iface_type == "bond":
                bond_count += 1
                bond_mode = iface.get("bond_mode", "")
                slaves = iface.get("slaves", "").split() if iface.get("slaves") else []
                bond_primary = iface.get("bond-primary", "")

                # Count active slaves by checking if interfaces exist in data
                active_slaves = []
                for slave in slaves:
                    slave_data = next((i for i in data if i.get("iface") == slave), None)
                    if slave_data and slave_data.get("active", 0):
                        active_slaves.append(slave)

                bond_detail = (
                    f"{len(active_slaves)}/{len(slaves)} members active (mode: {bond_mode})"
                )
                # Add primary member info for active-backup mode
                if bond_primary:
                    primary_active = bond_primary in active_slaves
                    primary_status = "active" if primary_active else "inactive"
                    bond_detail += f", primary: {bond_primary} ({primary_status})"

                if not active_slaves and slaves:
                    interfaces_down.append(iface_name)
                    entries.append(
                        (CheckState.CRITICAL, f"Interface '{iface_name}' is down: {bond_detail}")
                    )
                elif len(active_slaves) < len(slaves):
                    bonds_degraded.append(iface_name)
                    entries.append(
                        (CheckState.WARNING, f"Bond '{iface_name}' degraded: {bond_detail}")
                    )
                else:
                    entries.append((CheckState.OK, f"Bond '{iface_name}' is up: {bond_detail}"))

            # Only alert on bridge-ports and non-slave interfaces
            elif iface_type in ("bridge", "vlan", "eth", "unknown") and not iface.get("slave", 0):
                if is_active:
                    entries.append((CheckState.OK, f"Interface '{iface_name}' is up"))
                else:
                    interfaces_down.append(iface_name)
                    entries.append((CheckState.CRITICAL, f"Interface '{iface_name}' is down"))

        if name and not found:
            self.check_result = CheckState.UNKNOWN
            self.check_message = (
                f"Network interface '{name}' not found on node '{self.options.node}'"
            )
            return

        if interfaces_down:
            self.check_result = CheckState.CRITICAL
        elif bonds_degraded:
            self.check_result = CheckState.WARNING
        else:
            self.check_result = CheckState.OK

        if name:
            if self.check_result == CheckState.OK:
                self.check_message = f"Network interface '{name}' is healthy"
            else:
                self.check_message = entries[0][1]
            return

        for state, text in entries:
            self.add_detail(text, state)

        if self.check_result == CheckState.OK:
            self.check_message = (
                f"All {len(entries)} network interfaces on node '{self.options.node}' are healthy"
            )
        else:
            problems = []
            if interfaces_down:
                problems.append(f"{len(interfaces_down)} of {len(entries)} interfaces down")
            if bonds_degraded:
                problems.append(f"{len(bonds_degraded)} of {bond_count} bonds degraded")
            self.check_message = ", ".join(problems)

    def check_task_queue(self) -> None:
        """Check cluster task queue for running and failed tasks."""
        url = self.get_url("cluster/tasks")
        tasks = self.request(url)

        if tasks is None:
            self.check_result = CheckState.UNKNOWN
            self.check_message = "Could not fetch task queue data from API"
            return

        # Filter by node if specified
        if self.options.node is not None:
            tasks = [t for t in tasks if t.get("node") == self.options.node]

        # Filter by time window if critical threshold is set
        delta = self.threshold_critical("delta")
        if delta is not None:
            now = datetime.now(timezone.utc).timestamp()
            tasks = [t for t in tasks if not delta.check(now - t.get("starttime", now))]

        # Separate tasks by status
        running_tasks = [t for t in tasks if "status" not in t]
        completed_tasks = [t for t in tasks if "status" in t]
        failed_tasks = [t for t in completed_tasks if t.get("status") != "OK"]

        # Count tasks by type
        task_types = {}
        for task in running_tasks:
            task_type = task.get("type", "unknown")
            task_types[task_type] = task_types.get(task_type, 0) + 1

        # Check against thresholds
        warning_threshold = self.threshold_warning("running")
        critical_threshold = self.threshold_critical("running")

        running_count = len(running_tasks)
        failed_count = len(failed_tasks)

        # Build message
        messages = []
        if self.options.node:
            messages.append(f"Node '{self.options.node}':")
        else:
            messages.append("Cluster:")

        messages.append(f"{running_count} tasks running")

        if task_types:
            type_details = ", ".join(
                [f"{count} {ttype}" for ttype, count in sorted(task_types.items())]
            )
            messages.append(f"({type_details})")

        if failed_count > 0:
            messages.append(f", {failed_count} of {len(completed_tasks)} tasks failed")

        self.check_message = " ".join(messages)

        for task in failed_tasks:
            self.add_detail(self._format_task(task, task.get("status")), CheckState.WARNING)
        for task in running_tasks:
            self.add_detail(self._format_task(task, "running"), CheckState.OK)

        # Add time window info if specified
        if delta is not None:
            self.check_message += f" within the last {delta.value}s"

        # Determine check result
        if failed_count > 0:
            self.check_result = CheckState.WARNING
        elif critical_threshold is not None and critical_threshold.check(running_count):
            self.check_result = CheckState.CRITICAL
        elif warning_threshold is not None and warning_threshold.check(running_count):
            self.check_result = CheckState.WARNING
        else:
            self.check_result = CheckState.OK

        # Add performance data
        self.add_perfdata("running_tasks", running_count, unit="")
        self.add_perfdata("failed_tasks", failed_count, unit="")

    @staticmethod
    def _format_task(task: Dict, status: str) -> str:
        """Format a cluster task as detail line."""
        started = datetime.fromtimestamp(task.get("starttime", 0)).strftime("%Y-%m-%d %H:%M:%S")
        guest = f" {task['id']}" if task.get("id") else ""
        return (
            f"{task.get('type', 'unknown')}{guest} on node '{task.get('node')}' "
            f"started {started}: {status}"
        )

    def check_certificate(self) -> None:
        """Check SSL certificate expiration for cluster nodes."""
        url = self.get_url("cluster/resources")
        resources = self.request(url)

        if not resources:
            self.check_result = CheckState.UNKNOWN
            self.check_message = "Could not fetch cluster resources from API"
            return

        # Filter to get only nodes
        nodes = [r for r in resources if r.get("type") == "node"]

        # Filter by specific node if specified
        if self.options.node is not None:
            nodes = [n for n in nodes if n.get("node") == self.options.node]

        if not nodes:
            self.check_result = CheckState.UNKNOWN
            if self.options.node:
                self.check_message = f"Node '{self.options.node}' not found"
            else:
                self.check_message = "No nodes found in cluster"
            return

        # Get certificate info for each node
        expiring_soon = []
        expired = []
        cert_info = []

        # Get thresholds (default: warning=30 days, critical=7 days)
        warning_days = self.threshold_warning(None)
        critical_days = self.threshold_critical(None)

        if warning_days is None:
            warning_days = CheckThreshold(30)
        if critical_days is None:
            critical_days = CheckThreshold(7)

        for node in nodes:
            node_name = node.get("node")
            cert_url = self.get_url(f"nodes/{node_name}/certificates/info")

            try:
                cert_data = self.request(cert_url)

                if not cert_data:
                    continue

                # Process certificates - check pveproxy-ssl.pem if present, otherwise pve-ssl.pem
                # pveproxy-ssl.pem is used if custom certificate is configured
                cert_to_check = None

                for cert in cert_data:
                    filename = cert.get("filename", "unknown")
                    if filename == "pveproxy-ssl.pem":
                        cert_to_check = cert
                        break
                    elif filename == "pve-ssl.pem":
                        cert_to_check = cert

                if not cert_to_check:
                    continue

                filename = cert_to_check.get("filename")
                notafter = cert_to_check.get("notafter")
                if not notafter:
                    continue

                # Calculate days until expiration
                expiry_date = datetime.fromtimestamp(notafter, tz=timezone.utc)
                now = datetime.now(timezone.utc)
                days_left = (expiry_date - now).days

                cert_info.append(
                    {
                        "node": node_name,
                        "filename": filename,
                        "days_left": days_left,
                        "expiry_date": expiry_date,
                    }
                )

                # Check against thresholds
                certificate = f"{node_name}/{filename}"
                expiry = expiry_date.strftime("%Y-%m-%d")
                if days_left < 0:
                    expired.append(certificate)
                    self.add_detail(f"{certificate} expired on {expiry}", CheckState.CRITICAL)
                    continue

                detail = f"{certificate} expires in {days_left} days on {expiry}"
                if critical_days.check(days_left, lower=True):
                    expiring_soon.append((node_name, filename, days_left, "CRITICAL"))
                    self.add_detail(detail, CheckState.CRITICAL)
                elif warning_days.check(days_left, lower=True):
                    expiring_soon.append((node_name, filename, days_left, "WARNING"))
                    self.add_detail(detail, CheckState.WARNING)
                else:
                    self.add_detail(detail, CheckState.OK)

            except Exception:
                # If we can't get cert info for a node, skip it
                continue

        # Determine check result
        total = len(cert_info)
        if expired or expiring_soon:
            if expired or any(c[3] == "CRITICAL" for c in expiring_soon):
                self.check_result = CheckState.CRITICAL
            else:
                self.check_result = CheckState.WARNING

            problems = []
            if expired:
                problems.append(f"{len(expired)} of {total} certificate(s) expired")
            if expiring_soon:
                problems.append(f"{len(expiring_soon)} of {total} certificate(s) expiring soon")
            self.check_message = ", ".join(problems)
        else:
            self.check_result = CheckState.OK
            if self.options.node:
                self.check_message = f"Certificate on node '{self.options.node}' is valid"
            else:
                self.check_message = f"All {total} certificate(s) on {len(nodes)} node(s) are valid"

        # Add performance data for minimum days left (no unit, just number of days)
        if cert_info:
            min_days = min(c["days_left"] for c in cert_info)
            self.add_perfdata(
                "days_left",
                min_days,
                unit="",
                warning=warning_days.value,
                critical=critical_days.value,
            )

    def check_storage(self, name: str) -> None:
        """Check if storage exists and return usage."""
        url = self.get_url(f"nodes/{self.options.node}/storage")
        data = self.request(url)

        if not any(s["storage"] == name for s in data):
            self.check_result = CheckState.CRITICAL
            self.check_message = f"Storage '{name}' doesn't exist on node '{self.options.node}'"
            return

        url = self.get_url(f"nodes/{self.options.node}/storage/{name}/status")
        self.check_api_value(url, f"Usage of storage '{name}' is")

    def check_version(self) -> None:
        """Check PVE version."""
        url = self.get_url("version")
        data = self.request(url)

        # Handle empty or malformed responses gracefully
        if not data or "version" not in data or not data.get("version"):
            self.check_result = CheckState.UNKNOWN
            self.check_message = "Unable to determine PVE version"
            return

        if self.options.min_version and version.parse(self.options.min_version) > version.parse(
            data["version"]
        ):
            self.check_result = CheckState.CRITICAL
            self.check_message = (
                f"Current PVE version '{data['version']}' "
                f"({data['repoid']}) is lower than the min. "
                f"required version '{self.options.min_version}'"
            )
        else:
            self.check_result = CheckState.OK
            self.check_message = (
                f"Your PVE instance version '{data['version']}' ({data['repoid']}) is up to date"
            )

    def _get_pool_members(self, pool: str) -> Optional[List[int]]:
        """Get a list of vmids, which are members of a given resource pool.

        Returns None if the pool members could not be fetched.
        NOTE: The request needs the Pool.Audit permission!
        """
        try:
            url = self.get_url(f"pools/{pool}")
            data = self.request(url, raise_error=True)
        except RequestError:
            return None

        return [member["vmid"] for member in data.get("members", [])]

    def check_vzdump_backup(self, name: Optional[str] = None) -> None:
        """Check for failed vzdump backup jobs."""
        tasks_url = self.get_url("cluster/tasks")
        tasks = self.request(tasks_url)
        tasks = [t for t in tasks if t["type"] == "vzdump"]

        # Filter by node id, if one is provided
        if self.options.node is not None:
            tasks = [t for t in tasks if t["node"] == self.options.node]

        # Filter by timestamp, if provided
        delta = self.threshold_critical("delta")
        if delta is not None:
            now = datetime.now(timezone.utc).timestamp()

            tasks = [t for t in tasks if not delta.check(now - t["starttime"])]

        # absent status = job still running
        tasks = [t for t in tasks if "status" in t]
        failed = 0
        for task in tasks:
            if task["status"] != "OK":
                failed += 1
                self.add_detail(self._format_task(task, task["status"]), CheckState.CRITICAL)
            else:
                self.add_detail(self._format_task(task, task["status"]), CheckState.OK)
        success = len(tasks) - failed
        self.check_message = f"{success} of {len(tasks)} backup tasks successful"

        if failed > 0:
            self.check_result = CheckState.CRITICAL
            self.check_message += f", {failed} failed"
        else:
            self.check_result = CheckState.OK
        if delta is not None:
            self.check_message += f" within the last {delta.value}s"

        if not self.options.ignore_no_backup:
            nbu_url = self.get_url("cluster/backup-info/not-backed-up")
            not_backed_up = self.request(nbu_url)

            if len(not_backed_up) > 0:
                guest_ids = []

                for guest in not_backed_up:
                    guest_ids.append(guest["vmid"])

                ignored_vmids = []
                unreadable_pools = []
                for pool in self.options.ignore_pools:
                    # ignore vms based on their membership of a certain pool
                    members = self._get_pool_members(pool)
                    if members is None:
                        unreadable_pools.append(pool)
                    else:
                        ignored_vmids += members

                if self.options.ignore_vmids:
                    # ignore vms based on their id
                    ignored_vmids = ignored_vmids + self.options.ignore_vmids

                remaining_not_backed_up = sorted(list(set(guest_ids) - set(ignored_vmids)))
                if len(remaining_not_backed_up) > 0:
                    if self.check_result not in [CheckState.CRITICAL, CheckState.UNKNOWN]:
                        self.check_result = CheckState.WARNING
                    self.check_message += (
                        f", {len(remaining_not_backed_up)} guest(s) not covered by any "
                        "backup schedule"
                    )
                    for vmid in remaining_not_backed_up:
                        self.add_detail(
                            f"Guest {vmid} is not covered by any backup schedule",
                            CheckState.WARNING,
                        )

                if unreadable_pools:
                    self.add_detail(
                        "Unable to fetch members of pool(s) "
                        + ", ".join(f"'{pool}'" for pool in unreadable_pools)
                        + ". Check if the name is correct and the role has the "
                        "'Pool.Audit' permission"
                    )

    def check_snapshot_age(self, idx: Optional[Union[str, int]]) -> None:
        """Check age of snapshots."""
        url = self.get_url(
            "cluster/resources",
        )
        data = self.request(url, params={"type": "vm"})

        warnings = []
        criticals = []
        checked = 0
        snapshots_exist = False
        found = False
        for vm in data:
            vm_type = "qemu"
            if vm["type"] == "lxc":
                vm_type = "lxc"
            vm_name = vm.get("name", None)
            vm_id = vm.get("vmid", None)

            if not self.options.node:
                node_name = vm.get("node", None)
            else:
                node_name = self.options.node
            if node_name != vm.get("node", None):
                continue
            if idx:
                if idx not in (vm_name, vm_id):
                    continue
                found = True
            url = self.get_url(f"nodes/{node_name}/{vm_type}/{vm_id}/snapshot")
            snapshots = self.request(url)

            for snapshot in snapshots:
                snapshot_name = snapshot.get("name", None)

                if snapshot_name == "current":
                    continue
                snapshots_exist = True

                threshold_name = f"snapshot_age_{vm_name}_{snapshot_name}"
                threshold_warning = self.threshold_warning(threshold_name)
                threshold_critical = self.threshold_critical(threshold_name)

                snapshot_time = snapshot.get("snaptime", None)
                snapshot_age = int(datetime.now(timezone.utc).timestamp()) - snapshot_time
                snap_time = datetime.fromtimestamp(snapshot_time).strftime("%Y-%m-%d %H:%M:%S")
                detail = f"{vm_id} ({vm_name}): snapshot '{snapshot_name}' taken on {snap_time}"
                checked += 1

                if threshold_critical is not None and snapshot_age > int(threshold_critical.value):
                    criticals.append(snapshot_name)
                    self.add_detail(detail, CheckState.CRITICAL)
                elif threshold_warning is not None and snapshot_age > int(threshold_warning.value):
                    warnings.append(snapshot_name)
                    self.add_detail(detail, CheckState.WARNING)
                else:
                    self.add_detail(detail, CheckState.OK)

        if idx and not found:
            self.check_result = CheckState.UNKNOWN
            self.check_message = f"VM or LXC '{idx}' not found"
        elif not snapshots_exist:
            self.check_result = CheckState.OK
            if idx:
                self.check_message = f"No snapshots of '{idx}' exist"
            else:
                self.check_message = "No snapshots exist"
        else:
            subject = f" of '{idx}'" if idx else ""
            if criticals or warnings:
                if criticals:
                    self.check_result = CheckState.CRITICAL
                else:
                    self.check_result = CheckState.WARNING
                self.check_message = (
                    f"{len(criticals) + len(warnings)} of {checked} snapshots{subject} "
                    "are above age thresholds"
                )
            else:
                self.check_result = CheckState.OK
                self.check_message = f"Age of all {checked} snapshots{subject} is OK"

    def check_memory(self) -> None:
        """Check memory usage of Proxmox VE node."""
        url = self.get_url(f"nodes/{self.options.node}/status")
        self.check_api_value(url, "Memory usage is", key="memory")

    def check_swap(self) -> None:
        """Check swap usage of Proxmox VE node."""
        url = self.get_url(f"nodes/{self.options.node}/status")
        self.check_api_value(url, "Swap usage is", key="swap")

    def check_cpu(self) -> None:
        """Check cpu usage of Proxmox VE node."""
        url = self.get_url(f"nodes/{self.options.node}/status")
        self.check_api_value(url, "CPU usage is", key="cpu")

    def check_io_wait(self) -> None:
        """Check io wait of Proxmox VE node."""
        url = self.get_url(f"nodes/{self.options.node}/status")
        self.check_api_value(url, "IO wait is", key="wait", perfkey="wait")

    def check_thresholds(
        self,
        values: Union[Dict[str, Union[int, float]], Union[int, float]],
        message: str,
        **kwargs: Dict,
    ) -> None:
        """Check numeric value against threshold for given metric name."""
        is_warning = False
        is_critical = False

        if not isinstance(values, dict):
            values = {None: values}

        for metric, value in values.items():
            value_warning = self.threshold_warning(metric)
            if value_warning is not None:
                is_warning = is_warning or value_warning.check(
                    value, kwargs.get("lowerValue", False)
                )

            value_critical = self.threshold_critical(metric)
            if value_critical is not None:
                is_critical = is_critical or value_critical.check(
                    value, kwargs.get("lowerValue", False)
                )

        if is_critical:
            self.check_result = CheckState.CRITICAL
            self.check_message = kwargs.get("messageCritical", message)
        elif is_warning:
            self.check_result = CheckState.WARNING
            self.check_message = kwargs.get("messageWarning", message)
        else:
            self.check_result = CheckState.OK
            self.check_message = message

    def scale_value(self, value: Union[int, float]) -> float:
        """Scale value according to unit."""
        if self.options.unit in self.UNIT_SCALE:
            return value / self.UNIT_SCALE[self.options.unit]

        raise ValueError("wrong unit")

    def threshold_warning(self, name: str) -> CheckThreshold:
        """Get warning threshold for metric name (empty if none)."""
        return self.options.threshold_warning.get(
            name, self.options.threshold_warning.get(None, None)
        )

    def threshold_critical(self, name: str) -> CheckThreshold:
        """Get critical threshold for metric name (empty if none)."""
        return self.options.threshold_critical.get(
            name, self.options.threshold_critical.get(None, None)
        )

    def get_value(
        self, value: Union[int, float], total: Optional[Union[int, float]] = None
    ) -> float:
        """Get value scaled or as percentage."""
        value = float(value)

        if total:
            value /= float(total) / 100
        else:
            value = self.scale_value(value)

        return round(value, 2)

    def add_perfdata(self, name: str, value: Union[int, float], **kwargs: Dict) -> None:
        """Add metric to perfdata output."""
        unit = kwargs.get("unit", "%")

        perfdata = f"{name}={value}{unit}"

        threshold_warning = self.threshold_warning(name)
        threshold_critical = self.threshold_critical(name)

        perfdata += ";"
        if threshold_warning:
            perfdata += str(threshold_warning.value)

        perfdata += ";"
        if threshold_critical:
            perfdata += str(threshold_critical.value)

        perfdata += ";" + str(kwargs.get("min", 0))
        perfdata += ";" + str(kwargs.get("max", ""))

        self.perfdata.append(perfdata)

    def get_perfdata(self) -> str:
        """Get perfdata string."""
        perfdata = ""

        if self.perfdata:
            perfdata = "|"
            perfdata += " ".join(self.perfdata)

        return perfdata

    def check(self) -> None:
        """Execute the real check command."""
        self.check_result = CheckState.OK

        if self.options.mode == "cluster":
            self.check_cluster_status()
        elif self.options.mode == "version":
            self.check_version()
        elif self.options.mode == "memory":
            self.check_memory()
        elif self.options.mode == "swap":
            self.check_swap()
        elif self.options.mode in ("io_wait", "io-wait"):
            self.check_io_wait()
        elif self.options.mode == "disk-health":
            self.check_disks()
        elif self.options.mode == "cpu":
            self.check_cpu()
        elif self.options.mode == "services":
            self.check_services()
        elif self.options.mode == "updates":
            self.check_updates()
        elif self.options.mode == "subscription":
            self.check_subscription()
        elif self.options.mode == "storage":
            self.check_storage(self.options.name)
        elif self.options.mode in ["vm", "vm_status", "vm-status"]:
            only_status = self.options.mode in ["vm_status", "vm-status"]

            if self.options.name:
                idx = self.options.name
            else:
                idx = self.options.vmid

            if self.options.expected_vm_status:
                self.check_vm_status(
                    idx, expected_state=self.options.expected_vm_status, only_status=only_status
                )
            else:
                self.check_vm_status(idx, only_status=only_status)
        elif self.options.mode == "replication":
            self.check_replication()
        elif self.options.mode == "ceph-health":
            self.check_ceph_health()
        elif self.options.mode == "zfs-health":
            self.check_zfs_health(self.options.name)
        elif self.options.mode == "zfs-fragmentation":
            self.check_zfs_fragmentation(self.options.name)
        elif self.options.mode == "backup":
            self.check_vzdump_backup(self.options.name)
        elif self.options.mode == "snapshot-age":
            if self.options.name:
                idx = self.options.name
            else:
                idx = self.options.vmid

            self.check_snapshot_age(idx)
        elif self.options.mode == "network-status":
            self.check_network_status(self.options.name)
        elif self.options.mode == "task-queue":
            self.check_task_queue()
        elif self.options.mode == "certificate":
            self.check_certificate()
        else:
            message = f"Check mode '{self.options.mode}' not known"
            self.output(CheckState.UNKNOWN, message)

        self.check_output()

    def parse_args(self, argv: Optional[List[str]] = None) -> argparse.Namespace:
        """Parse CLI arguments.

        Accept an optional argv list for testing convenience and return the parsed
        options namespace.
        """
        p = CheckArgumentParser(description="Check command for PVE hosts via API")

        p.add_argument(
            "--version", help="Show version of check command", action="store_true", default=False
        )

        api_opts = p.add_argument_group("API Options")

        api_opts.add_argument(
            "-e",
            "-H",
            "--api-endpoint",
            help="PVE api endpoint hostname or IP address (no additional data like paths)",
        )
        api_opts.add_argument("--api-port", required=False, help="PVE api endpoint port")

        api_opts.add_argument(
            "-u",
            "--username",
            dest="api_user",
            help="PVE api user (e.g. icinga2@pve or icinga2@pam, depending on which backend you "
            "have chosen in proxmox)",
        )

        group = api_opts.add_mutually_exclusive_group()
        group.add_argument("-p", "--password", dest="api_password", help="PVE API user password")
        group.add_argument(
            "-P",
            "--password-file",
            dest="api_password_file",
            help="PVE API user password in a file",
        )
        group.add_argument(
            "-t",
            "--api-token",
            dest="api_token",
            help="PVE API token (format: TOKEN_ID=TOKEN_SECRET)",
        )
        group.add_argument(
            "-T",
            "--api-token-file",
            dest="api_token_file",
            help="PVE API token contained in a file (format: TOKEN_ID=TOKEN_SECRET)",
        )

        api_opts.add_argument(
            "-k",
            "--insecure",
            dest="api_insecure",
            action="store_true",
            default=False,
            help="Don't verify HTTPS certificate",
        )

        api_opts.set_defaults(api_port=8006)

        check_opts = p.add_argument_group("Check Options")

        check_opts.add_argument(
            "-m",
            "--mode",
            choices=(
                "cluster",
                "version",
                "cpu",
                "memory",
                "swap",
                "storage",
                "io_wait",
                "io-wait",
                "updates",
                "services",
                "subscription",
                "vm",
                "vm_status",
                "vm-status",
                "replication",
                "disk-health",
                "ceph-health",
                "zfs-health",
                "zfs-fragmentation",
                "backup",
                "snapshot-age",
                "network-status",
                "task-queue",
                "certificate",
            ),
            help="Mode to use.",
        )

        check_opts.add_argument(
            "-n",
            "--node",
            dest="node",
            help="Node to check (necessary for all modes except cluster, version and backup)",
        )

        check_opts.add_argument("--name", dest="name", help="Name of storage, vm, or container")

        check_opts.add_argument(
            "--vmid", dest="vmid", type=int, help="ID of virtual machine or container"
        )

        check_opts.add_argument(
            "--expected-vm-status",
            choices=("running", "stopped", "paused"),
            help="Expected VM status",
        )

        check_opts.add_argument(
            "--ignore-vmid",
            dest="ignore_vmids",
            metavar="VMID",
            action="append",
            help="Ignore VM with vmid in checks",
            default=[],
            type=int,
        )

        check_opts.add_argument(
            "--ignore-vm-status",
            dest="ignore_vm_status",
            action="store_true",
            help="Ignore VM status in checks",
            default=False,
        )

        check_opts.add_argument(
            "--ignore-service",
            dest="ignore_services",
            action="append",
            metavar="NAME",
            help="Ignore service NAME in checks",
            default=[],
        )

        check_opts.add_argument(
            "--ignore-disk",
            dest="ignore_disks",
            action="append",
            metavar="DISK",
            help="Ignore disk DISK in health check. Accepts either the device "
            "name (e.g. 'sdb') or the disk's serial number; matching is "
            "case-insensitive. Can be given multiple times.",
            default=[],
        )

        check_opts.add_argument(
            "--ignore-pools",
            dest="ignore_pools",
            action="append",
            metavar="NAME",
            help="Ignore VMs and containers in pool(s) NAME in checks",
            default=[],
        )

        check_opts.add_argument(
            "--ignore-no-backup",
            dest="ignore_no_backup",
            action="store_true",
            help="Ignore not backed up VMs in backup check",
            default=False,
        )

        check_opts.add_argument(
            "--ignore-interface",
            dest="ignore_interfaces",
            action="append",
            metavar="NAME",
            help="Ignore network interface NAME in network status check",
            default=[],
        )

        check_opts.add_argument(
            "--detail",
            dest="detail",
            action="store_true",
            default=False,
            help="Also list items in OK state in the detail lines below the summary",
        )

        check_opts.add_argument(
            "-w",
            "--warning",
            dest="threshold_warning",
            type=CheckThreshold.threshold_type,
            default={},
            help="Warning threshold for check value. "
            "Multiple thresholds with name:value,name:value",
        )
        check_opts.add_argument(
            "-c",
            "--critical",
            dest="threshold_critical",
            type=CheckThreshold.threshold_type,
            default={},
            help=(
                "Critical threshold for check value. "
                "Multiple thresholds with name:value,name:value"
            ),
        )
        check_opts.add_argument(
            "-M",
            dest="values_mb",
            action="store_true",
            default=False,
            help=(
                "Values are shown in the unit which is set with --unit (if available). "
                "Thresholds are also treated in this unit"
            ),
        )
        check_opts.add_argument(
            "-V",
            "--min-version",
            dest="min_version",
            type=str,
            help="The minimum PVE version to check for. Any version lower than this will return "
            "CRITICAL.",
        )

        check_opts.add_argument(
            "--unit",
            choices=self.UNIT_SCALE.keys(),
            default="MiB",
            help="Unit which is used for performance data and other values",
        )

        options = p.parse_args(argv)

        if options.version:
            print(f"check_pve version {self.VERSION}")
            sys.exit(0)

        missing = []
        if not options.api_endpoint:
            missing.append("--api-endpoint")
        if not options.api_user:
            missing.append("--username")
        if not (
            options.api_password
            or options.api_password_file
            or options.api_token
            or options.api_token_file
        ):
            missing.append("--password, --api-password-file, --api-token or --api-token-file")
        if not options.mode:
            missing.append("--mode")

        if missing:
            p.error(f"The following arguments are required: {', '.join(missing)}")

        if not options.node and options.mode not in [
            "cluster",
            "vm",
            "vm_status",
            "vm-status",
            "version",
            "ceph-health",
            "backup",
            "snapshot-age",
            "task-queue",
            "certificate",
        ]:
            p.error(f"--mode {options.mode} requires node name (--node)")

        if (
            not options.vmid
            and not options.name
            and options.mode in ("vm", "vm_status", "vm-status")
        ):
            p.error(f"--mode {options.mode} requires either vm name (--name) or id (--vmid)")

        if not options.name and options.mode == "storage":
            p.error(f"--mode {options.mode} requires storage name (--name)")

        if options.threshold_warning and options.threshold_critical:
            if options.mode not in ["subscription", "certificate"] and not compare_thresholds(
                options.threshold_warning, options.threshold_critical, lambda w, c: w <= c
            ):
                p.error("Critical value must be greater than warning value")
            elif options.mode in ["subscription", "certificate"] and not compare_thresholds(
                options.threshold_warning, options.threshold_critical, lambda w, c: w >= c
            ):
                p.error("Critical value must be lower than warning value")

        self.options = options
        return options

    def __init__(self) -> None:
        self.options = {}
        self.ticket = None
        self.perfdata = []
        self.details = []
        self.check_result = CheckState.UNKNOWN
        self.check_message = ""

        self.__headers = {}
        self.__cookies = {}

        self.parse_args()

        if self.options.api_insecure:
            # disable urllib3 warning about insecure requests
            requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

        if self.options.api_token_file is not None:
            self.options.api_token = self.get_file_line(self.options.api_token_file)
        if self.options.api_password_file is not None:
            self.options.api_password = self.get_file_line(self.options.api_password_file)
        if self.options.api_password is not None:
            self.__cookies["PVEAuthCookie"] = self.get_ticket()
        elif self.options.api_token is not None:
            token = f"{self.options.api_user}!{self.options.api_token}"
            self.__headers["Authorization"] = f"PVEAPIToken={token}"


def main() -> None:
    """Run the check and map any unexpected error to UNKNOWN."""
    try:
        pve = CheckPVE()
        pve.check()
    except Exception as e:
        # Uncaught exceptions would exit with 1 (WARNING); details are omitted
        # from the output as they may expose internals.
        CheckPVE.output(CheckState.UNKNOWN, f"Unexpected error: {type(e).__name__}")


if __name__ == "__main__":
    main()
