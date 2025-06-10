#!/usr/bin/env python

import logging
import sys
from pyats import aetest
from pyats.topology import loader
from unicon.core.errors import TimeoutError, StateMachineError, ConnectionError

# Add custom genie parsers path
sys.path.insert(0, 'custom_genie_parsers/')
from custom_genie_parsers.show_bgp import ShowBgpVrf, ShowBgpAddressFamily

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def fix_ipv6_raw_output(raw_output):
    """Normalize multi-line IPv6 'show bgp' into parsable lines."""
    lines = raw_output.splitlines()
    valid_lines = []
    prev = ""
    found = False
    header = []

    for ln in lines:
        if 'Route Distinguisher Version' in ln or ('Network' in ln and 'Metric' in ln):
            header.append(ln)
            found = True
            continue
        if not found:
            header.append(ln)
            continue

        ln = ln.rstrip()
        if ln.startswith('*'):
            if prev:
                valid_lines.append(prev)
            prev = ln
        else:
            if 'Processed' not in ln:
                prev += " " + ln.strip()

    if prev:
        valid_lines.append(prev)

    return "\n".join(header + valid_lines)


class CommonSetup(aetest.CommonSetup):

    @aetest.subsection
    def load_testbed(self, testbed):
        """Convert testbed YAML -> Genie Testbed instance."""
        self.parent.parameters['testbed'] = loader.load(testbed)
        logger.info("Loaded testbed %s", testbed)

    @aetest.subsection
    def connect(self, testbed, host):
        """Connect to the specified device."""
        tb = self.parent.parameters['testbed']
        device = tb.devices.get(host)
        assert device, f"Host '{host}' not found in testbed"
        try:
            device.connect(
                timeout=60,
                log_stdout=False,
                init_exec_commands=[],
                init_config_commands=[]
            )
            self.parent.parameters['device'] = device
            logger.info("Connected to %s", host)
        except (TimeoutError, StateMachineError, ConnectionError) as e:
            self.failed(f"Could not connect to {host}: {e}")


class BgpTable(aetest.Testcase):
    """Retrieve and verify BGP routing table."""

    @aetest.setup
    def setup(self, device, vrf, filter):
        """Run 'show bgp' and parse."""
        af = filter.replace('_', ' ').replace('-', ' ')
        logger.info("Using AF '%s', VRF '%s'", af, vrf or 'default')

        cmd = (
            f"show bgp vrf {vrf}"
            if vrf and 'ipv4' in af
            else f"show bgp {af} vrf {vrf}"
                if vrf
            else f"show bgp {af}"
        )

        raw = device.execute(cmd)
        if not raw.strip():
            self.failed("No output from device (BGP may be down)")

        if 'v6' in af:
            raw = fix_ipv6_raw_output(raw)

        parser = ShowBgpVrf(device=device) if 'vrf' in cmd else ShowBgpAddressFamily(device=device)
        parsed = parser.parse(output=raw)
        logger.info("Parsed BGP data OK")
        self.parent.parameters['parsed'] = parsed

    @aetest.test
    def verify_bgp_table(self, parsed):
        """Fail if we didn’t actually see any prefixes."""
        # Drill into your parsed dict to reach the 'prefix' maps ...
        # we assume the top-level is either {'address_family': {...}} or {'vrf': {'<name>': {'address_family': {...}}}}
        # Simplest: flatten to the dict that contains 'prefix'
        af_data = None
        if 'address_family' in parsed:
            af_data = parsed['address_family']
        elif 'vrf' in parsed:
            # pick the only VRF key
            vrf_key = next(iter(parsed['vrf']))
            af_data = parsed['vrf'][vrf_key]['address_family']
        else:
            self.failed("Parsed BGP structure unexpected")

        prefixes = []
        for af, details in af_data.items():
            prefixes.extend(details.get('prefix', {}).keys())

        logger.info("Found %d prefixes", len(prefixes))

        if not prefixes:
            self.failed("No BGP prefixes learned")

        # if you'd like to fail on specific criteria, do it here
        # e.g. if len(prefixes)<X: self.failed(...)


class CommonCleanup(aetest.CommonCleanup):

    @aetest.subsection
    def disconnect(self):
        try:
            """Disconnect from the device at the end of the test run."""
            device = self.parent.parameters.get('device')
            if device and device.connected:
                logger.info(f"Disconnecting from device {device.name}")
                device.disconnect()
        except Exception as e:
            logger.error(f"Error while disconnecting from the device/s: {e}")
            self.failed(f"Error while disconnecting from the device/s: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch & verify a device's BGP table")
    parser.add_argument(
        "--testbed", "-t", required=True,
        help="Testbed YAML file"
    )
    parser.add_argument(
        "--host", "-d", required=True,
        help="Device name (as in testbed)"
    )
    parser.add_argument(
        "--vrf", "-v", default="",
        help="VRF name (omit for default VRF)"
    )
    parser.add_argument(
        "--filter", "-f", default="ipv4_unicast",
        help="Address-family filter (e.g. ipv4_unicast, ipv6_unicast)"
    )

    args = parser.parse_args()

    # Load & pass into AEtest
    tb = args.testbed
    hv = dict(
        testbed=tb,
        host=args.host,
        vrf=args.vrf,
        filter=args.filter
    )

    aetest.main(**hv)
