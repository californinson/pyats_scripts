"""
configure_bgp_neighbor.py

Test script to configure and verify a BGP neighbor on a Cisco IOS-XR device.

This script does the following:
  1. Connects to the target device (using credentials from environment).
  2. Runs a 'show bgp [<address-family>] vrf <VRF> neighbors' command.
  3. If the requested neighbor IP is already present, immediately PASS the test.
  4. Otherwise, push the BGP neighbor configuration (under 'router bgp <ASN> …').
  5. Re-run the show command up to 3 times to confirm the neighbor appears.
  6. If the neighbor appears, PASS; else FAIL.

Usage (via pyats job):
  pyats run job network_test.py --testbed devices_v2.yaml \\
    --host er11.test-uk.bllab --vrf uk-sns-ddos-lab \\
    --neighbor 10.10.10.1 --bgp_neighbor_data "router bgp 65000/ neighbor 10.10.10.1 remote-as 65001"

Assumptions:
  - The Genie 'ShowBgpNeighbors' parser works on the target device.
  - Default BGP instance is used.
  - 'neighbor_ip' and 'bgp_neighbor_data' are provided via CLI or jobfile.
"""
import logging
from time import sleep
from pyats import aetest
from pyats.topology import loader  # using loader to handle testbed loading if needed
from unicon.core.errors import TimeoutError, StateMachineError, ConnectionError
from genie.libs.parser.iosxr.show_bgp import ShowBgpNeighbors
import os

# Create a logger for this module
logger = logging.getLogger(__name__)

class CommonSetup(aetest.CommonSetup):
    """
    Common setup for configuring a new BGP neighbor.

    Responsibilities:
      - Load or verify the testbed object.
      - Connect to the specified device (with credentials from env).
      - Update shared parameters: 'device', 'host', and 'testbed'.
    """
    @aetest.subsection
    def load_testbed(self, testbed):
        """
        Convert or verify the 'testbed' argument into a Genie Testbed
        object and store it in shared parameters.

        :param testbed: Either a path to a YAML file or an existing
                        Testbed object.
        """
        logger.info("Loading testbed information.")
        # Avoid re-loading if testbed is already an object
        if not hasattr(testbed, 'devices'):
            testbed = loader.load(testbed)
        self.parent.parameters.update(testbed=testbed)
        assert testbed, "Testbed is not provided!"

    @aetest.subsection
    def connect(self, testbed):
        """
        Connect to the device specified by 'host'. Expects 'host'
        parameter to be passed in at runtime.

        :param testbed: The Genie Testbed object.
        :raises ValueError: If 'host' is not provided or not found.
        :raises self.failed: If connection to the device fails.
        """
        host = self.parent.parameters.get('host', None)
        if not host:
            raise ValueError("Missing host parameter")
        assert testbed, "Testbed is not provided!"

        device = testbed.devices.get(host)
        if not device:
            raise ValueError(f"Host '{host}' not found in testbed")

        try:
            device = testbed.devices[host]

            # If credentials are provided via environment variables, set them
            username = os.environ.get('USERNAME') or os.environ.get('PYATS_USERNAME')
            password = os.environ.get('PASSWORD') or os.environ.get('PYATS_PASSWORD')
            if username and password:
                device.credentials['default'] = {
                    'username': username,
                    'password': password
                }

            # Connect to the device
            device.connect(
                timeout=60,
                log_stdout=False,
                init_exec_commands=[],
                init_config_commands=[]
            )
            logger.info(f"Successfully connected to device {device.name}")

            # Save the connected device in testscript parameters for later use
            self.parent.parameters.update(device=device, host=host)
        except (TimeoutError, StateMachineError, ConnectionError) as e:
            logger.error(f"Unable to connect to device {host}: {e}")
            # Mark the setup as failed if connection fails
            self.failed(f"Failed to connect to device {host}: {e}")

class ConfigureBGPNeighbor(aetest.Testcase):
    """
    Testcase to add (or verify existing) a BGP neighbor on a Cisco device.

    Steps:
      1. verify_bgp_neighbors (setup): Check if neighbor already exists.
         - If yes: call self.passed(...) and return early.
         - Otherwise: stash necessary parameters (command, vrf_item, found=False).
      2. add_neighbor_config (test): If found==True, call self.skipped(...).
         Otherwise, push configuration and verify neighbor appears.
    """

    @aetest.setup
    def verify_bgp_neighbors(self, device, vrf, filter, neighbor_ip):
        """
        Execute 'show bgp ... neighbors', parse output, and detect
        if 'neighbor_ip' is already configured.

        :param device:        Connected Genie device object.
        :param vrf:           VRF name (string). Empty = default VRF.
        :param filter:        Address-family filter (e.g., 'ipv4_unicast').
        :param neighbor_ip:   IP address of the BGP neighbor to add.
        :raises self.failed: If the device does not respond or parse error.
        :raises self.passed: If neighbor already exists (early exit).
        """
        try:
            # Normalize address-family filter string
            filter = filter.replace('-', ' ').replace('_', ' ')
            logger.info(f"Using address-family filter: '{filter}', VRF: '{vrf or 'default'}'")

            # Construct the appropriate show command based on VRF and filter
            if 'ipv4' in filter:
                command = f"show bgp vrf {vrf} neighbors" if vrf else "show bgp neighbors"
            else:
                command = f"show bgp {filter} vrf {vrf} neighbors" if vrf else f"show bgp {filter} neighbors"

            logger.info(f"Executing command on device {device.name}: {command}")
            raw_output = device.execute(command)
            logger.info(f"Raw output received from {device.name}:\n{raw_output}")

            # Check if raw_output is empty (indicating an error with the device)
            if not raw_output.strip():
                self.failed(f"No response from the device, likely due to communication being down.")

            # Parse the raw output using the appropriate Genie parser
            bgp_neighbors = ShowBgpNeighbors(device=device)
            parsed_output = bgp_neighbors.parse(output=raw_output)
            logger.info(f"Parsed output: {parsed_output}")

            # Check if any of the neighbors already exist
            if (vrf):
                vrf_item = vrf
            else:
                vrf_item = 'default'

            existing_neighbors = parsed_output.get("instance", {}).get("all", {}).get("vrf", {}).get(vrf_item, {}).get(
                "neighbor", {})

            if neighbor_ip in existing_neighbors:
                logger.info(f"BGP neighbor {neighbor_ip} already exists.")
                self.passed(f"Neighbor {neighbor_ip} already exists.")

            # Store parsed output in parameters for use in test step
            self.parent.parameters.update(command=command)
            self.parent.parameters.update(vrf_item=vrf_item)
        except Exception as e:
            logger.error(f"Error processing BGP neighbors: {e}")
            # Fail the testcase setup if parsing or execution fails
            self.failed(f"Setup failed: unable to get BGP neighbors - {e}")

    @aetest.test
    def add_neighbor_config(self, device, neighbor_ip, bgp_neighbor_data, command, vrf_item):
        """
        Add 'neighbor_ip' as a new BGP neighbor if not already present,
        then recheck until present (up to 3 tries). Fail if still absent.

        :param device:             Connected Genie device object.
        :param neighbor_ip:        IP address of the BGP neighbor to add.
        :param bgp_neighbor_data:  String with BGP neighbor config lines,
                                   delimited by '/' (e.g. "router bgp 65000/
                                   neighbor 10.10.10.1 remote-as 65001").
        :param command:            The original 'show bgp ... neighbors' command.
        :param vrf_item:           The VRF key in parsed dict (string).
        :param found:              Boolean flag from setup (always False here).
        :raises self.skipped:      If found==True (neighbor already present).
        :raises self.failed:       If neighbor not found after config.
        """
        try:
            logger.info(f"Adding new BGP neighbor {neighbor_ip}")

            config_commands=bgp_neighbor_data.split('/')

            logger.info(f"New config is{bgp_neighbor_data}")

            device.configure(config_commands)
            logger.info(f"Configuration added on the device. Checking if new neighbor is loaded...")

            found=False
            attempts=0
            while(not found or attempts<=3):
                logger.info(f"Attempt #{attempts}")

                logger.info(f"      Executing command on device {device.name}: {command}")
                raw_output = device.execute(command)
                logger.info(f"      Raw output received from {device.name}:\n{raw_output}")

                # Check if raw_output is empty (indicating an error with the device)
                if not raw_output.strip():
                    self.failed(f"      No response from the device, likely due to communication being down.")

                # Parse the raw output using the appropriate Genie parser
                bgp_neighbors = ShowBgpNeighbors(device=device)
                parsed_output = bgp_neighbors.parse(output=raw_output)

                existing_neighbors = parsed_output.get("instance", {}).get("all", {}).get("vrf", {}).get(vrf_item, {}).get(
                    "neighbor", {})

                if neighbor_ip in existing_neighbors:
                    found=True

                attempts+=1
                sleep(2)

            if found:
                logger.info(f"New neighbor {neighbor_ip} was configured successfully")
            else:
                logger.error(f"New neighbor {neighbor_ip} was NOT configured. Please try again.")
                self.failed(f"New neighbor {neighbor_ip} was NOT configured. Please try again.")

        except Exception as e:
            logger.error(f"Error while adding new BGP neighbor configuration: {e}")
            self.failed(f"Error while adding new BGP neighbor configuration: {e}")

class CommonCleanup(aetest.CommonCleanup):
    """
    Common cleanup: disconnect from the device if still connected.
    """
    @aetest.subsection
    def disconnect(self):
        """Disconnect from the device at the end of the test run."""
        device = self.parent.parameters.get('device')
        if device and device.connected:
            logger.info(f"Disconnecting from device {device.name}")
            device.disconnect()
