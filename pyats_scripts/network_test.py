import os
import sys
from pyats.easypy import run
from genie.testbed import load

# Compute the absolute path to this directory (for referencing test scripts)
SCRIPT_PATH = os.path.dirname(__file__)

def main(runtime):
    """Job file entry point for running network tests."""
    # Initialize parameter variables
    host = None
    vrf = ''
    filter_af = 'ipv4_unicast'
    testbed_path = None
    neighbor_ip=None
    bgp_neighbor_data=None

    # Check if the testbed is already loaded by runtime (provided via --testbed)
    testbed = getattr(runtime, 'testbed', None)
    try:
        # In some versions, runtime.testbed may be directly accessible
        if runtime.testbed:
            testbed = runtime.testbed
    except Exception:
        pass

    # Parse command-line arguments for host, vrf, filter, and testbed (if not already handled)
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == '--testbed' and i + 1 < len(argv):
            testbed_path = argv[i + 1]
        elif arg == '--host' and i + 1 < len(argv):
            host = argv[i + 1]
        elif arg == '--vrf' and i + 1 < len(argv):
            vrf = argv[i + 1]
        elif arg == '--filter' and i + 1 < len(argv):
            filter_af = argv[i + 1]
        elif arg == '--neighbor' and i + 1 < len(argv):
            neighbor_ip=argv[i + 1]

    # Load testbed from file path if it wasn't already loaded
    if testbed is None:
        if testbed_path:
            testbed = load(testbed_path)
        else:
            raise Exception("No testbed provided. Use --testbed <testbed_file.yaml> to specify the testbed.")

    # Require a host to be specified
    if not host:
        raise Exception("No host provided. Use --host <device_name> to specify the target device.")

    # Run the BGP table test script with the current parameters
    run(
        testscript=os.path.join(SCRIPT_PATH, "get_bgp_table.py"),
        runtime=runtime,
        taskid="BGP Table",
        testbed=testbed,
        host=host,
        vrf=vrf,
        filter=filter_af
    )

    if(not neighbor_ip):
        raise Exception("Neighbor IP not provided. Skipping new neighbor configuration. Use --neighbor <neighbor_ip> "
                        "to specify the new BGP neighbor config.")

    if(not bgp_neighbor_data):
        raise Exception("New BGP config not provided. Skipping new neighbor configuration. Use --bgpdata <new/data/here> "
                        "to specify the new BGP neighbor config.")

    run(
        testscript=os.path.join(SCRIPT_PATH, "configure_bgp_neighbor.py"),
        runtime=runtime,
        taskid="New BGP Neighbor",
        testbed=testbed,
        host=host,
        vrf=vrf,
        filter=filter_af,
        neighbor_ip=neighbor_ip,
        bgp_neighbor_data=bgp_neighbor_data
    )
