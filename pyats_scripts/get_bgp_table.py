from pyats.easypy import runtime
import logging
import sys
from pyats import aetest
from pyats.topology import loader  # using loader to handle testbed loading if needed
from unicon.core.errors import TimeoutError, StateMachineError, ConnectionError
import os
from CloudflareAiAgent import CloudflareAIAgent
from AiAgent import AIAgent

# Add custom genie parsers path to the system path
sys.path.insert(0, 'custom_genie_parsers/')

# Import custom genie parsers (modified genie parsers for BGP)
from custom_genie_parsers.show_bgp import ShowBgpVrf, ShowBgpAddressFamily

# Create a logger for this module
logger = logging.getLogger(__name__)

# Fix method for IPv6 raw output (concatenates lines for proper parsing)
def fix_ipv6_raw_output(raw_output):
    lines = raw_output.splitlines()
    valid_lines = []
    previous_line = ""
    found = False
    original_text = []

    for line in lines:
        if 'Route Distinguisher' in line or ('Network' in line and 'Metric' in line):
            original_text.append(line)
            found = True
            continue
        else:
            if not found:
                original_text.append(line)

        if found:
            line = line.rstrip()
            if line.startswith('*'):
                if previous_line:
                    valid_lines.append(previous_line)
                previous_line = line
            else:
                if 'Processed' not in line:
                    previous_line += " " + line.strip()

    if previous_line:
        valid_lines.append(previous_line)

    original_text = "\n".join(original_text)
    valid_lines = "\n".join(valid_lines)
    return original_text + '\n' + valid_lines

def ai_agent_choice():
    ### This function will check what environment variable the user has passed ###
    ### if the user has chosen to use RunPod, proper Pod and Pod port will be provided ###
    ### Otherwise Cludflare Worker AI model full URL and Cloudflare API key are  expected ###

    runpod_host = os.environ.get('RUNPOD_HOST') or None
    runpod_host_port = os.environ.get('RUNPOD_HOST_PORT') or None

    cloudlfare_host = os.environ.get('AI_HOST') or None
    cloudlfare_api_key = os.environ.get('API_KEY') or None

    if(runpod_host and not cloudlfare_host and runpod_host_port):
        ai_host=runpod_host
        ai_host_port=runpod_host_port

        system_prompt = (
            "### Role: You are a senior network engineer.\n"
            "### Task: Evaluate and summarize network output from a Cisco IOS XR device using bullet points.\n\n"
        )
        return 0, {'ai_host':ai_host, 'ai_host_port': ai_host_port, 'system_prompt': system_prompt}
    else:
        if(not runpod_host and cloudlfare_host and cloudlfare_api_key):
            ai_host = cloudlfare_host
            api_key = cloudlfare_api_key

            system_prompt = {"role": "system", "content": "You are a senior network engineer. "
                                                          "Evaluate and summarize network output from a Cisco IOS XR "
                                                          "device using bullet points"
                             }

            return 1, {'ai_host': ai_host, 'api_key': api_key, 'system_prompt': system_prompt}
        else:
            return -1

class CommonSetup(aetest.CommonSetup):
    @aetest.subsection
    def load_system(self, testbed):
        """Convert/verify testbed and store it in parameters."""
        logger.info("Loading testbed information.")
        # Avoid re-loading if testbed is already an object
        if not hasattr(testbed, 'devices'):
            testbed = loader.load(testbed)
        self.parent.parameters.update(testbed=testbed)
        assert testbed, "Testbed is not provided!"

        # create ai agent instance
        code, ai_dict=ai_agent_choice()

        ai_agent = None

        try:
            if(code==0):
                logger.info(f"Setting up AI Agent with http://{ai_dict['ai_host']}:{ai_dict['ai_host_port']}")

                ai_agent = AIAgent(
                    ai_host=ai_dict['ai_host'],
                    ai_host_port=ai_dict['ai_host_port'],
                    system_prompt=ai_dict['system_prompt']
                )
            else:
                if(code==1):
                    logger.info(f"Setting up AI Agent with Cloudflare {ai_dict['ai_host']}")

                    ai_agent = CloudflareAIAgent(
                        ai_host=ai_dict['ai_host'],
                        api_key=ai_dict['api_key'],
                        system_prompt=ai_dict['system_prompt']
                    )
                else:
                    logger.warning(f"Error while setting up AI Agent. Check env parameters")
        except Exception as e:
            logger.warning(f"Error while setting up AI Agent. {e}")

        self.parent.parameters.update(ai_agent=ai_agent)

    @aetest.subsection
    def connect_testbed(self, testbed):
        """Connect to the device specified by host and store the device object."""
        # Retrieve host from parameters (set via job or CLI)
        host = self.parent.parameters.get('host', None)
        if not host:
            raise ValueError("Missing host parameter")
        assert testbed, "Testbed is not provided!"

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

                self.parent.parameters.update(device_user=username)

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

class BgpTable(aetest.Testcase):
    """Testcase to retrieve and verify BGP routing table information."""
    @aetest.setup
    def setup(self, device, vrf, filter):
        """Execute BGP show command and parse the output."""
        try:
            # Normalize address-family filter string
            filter = filter.replace('-', ' ').replace('_', ' ')
            logger.info(f"Using address-family filter: '{filter}', VRF: '{vrf or 'default'}'")

            # Construct the appropriate show command based on VRF and filter
            if 'ipv4' in filter:
                command = f"show bgp vrf {vrf}" if vrf else "show bgp"
            else:
                command = f"show bgp {filter} vrf {vrf}" if vrf else f"show bgp {filter}"

            logger.info(f"Executing command on device {device.name}: {command}")
            raw_output = device.execute(command)
            logger.info(f"Raw output received from {device.name}:\n{raw_output}")

            # Check if raw_output is empty (indicating BGP may be down)
            if not raw_output.strip():
                self.failed(f"BGP table is empty, likely due to BGP being down on the device.")

            # If IPv6 address-family, fix the raw output formatting for parsing
            if 'v6' in filter:
                raw_output = fix_ipv6_raw_output(raw_output)

            # Parse the raw output using the appropriate Genie parser
            if 'vrf' not in command:
                parser = ShowBgpAddressFamily(device=device)
            else:
                parser = ShowBgpVrf(device=device)
            parsed_output = parser.parse(output=raw_output)
            logger.info(f"Parsed output: {parsed_output}")

            #Read ai agent parameter from parent.parameters
            ai_agent = self.parent.parameters.get('ai_agent') or None

            if(ai_agent):
                device_user=self.parent.parameters.get('device_user','unknown')

                prompt=("Analyse and do a health check of this BGP table. Also check if RTBH is activated for any of the "
                        "/32 host routes")

                #call ai agent generate class to analyse bgp table
                ok, raw_output_summary=ai_agent.generate(device=device.name,user=device_user,
                                                           raw_output=raw_output, prompt=prompt)

                if(ok):
                    ok, final_analysis=ai_agent.get_final_response(device=device.name,user=device_user)

                    if(ok):
                        logger.info("🔎 AI summary:\n%s", final_analysis)
                        # Store AI final analysis in parameters for use in cleanup step
                        self.parent.parameters.update(final_analysis=final_analysis)
                    else:
                        logger.error(f"AI analysis failed: {final_analysis}\n")
                        self.parent.parameters.update(final_analysis=None)
                else:
                    logger.error(f"AI summary failed: {raw_output_summary}\n")
            else:
                logger.warning(f"Error while setting up AI Agent. Check env parameters")

            # Store parsed output in parameters for use in test step
            self.parent.parameters.update(parsed_output=parsed_output)
            self.parent.parameters.update(output_dir=runtime.directory)
        except Exception as e:
            logger.error(f"Error processing BGP table: {e}")
            # Fail the testcase setup if parsing or execution fails
            self.failed(f"Setup failed: unable to get BGP table - {e}")

    @aetest.test
    def verify_bgp_table(self, parsed_output):
        """Verify or extract information from the parsed BGP table."""
        extracted_data = []
        try:
            # Iterate over parsed BGP data to extract relevant fields
            for af, af_data in parsed_output.items():
                if 'prefix' in af_data:
                    for prefix, details in af_data['prefix'].items():
                        for idx, info in details.get('index', {}).items():
                            extracted_data.append({
                                'network': prefix,
                                'next_hop': info.get('next_hop', ''),
                                'metric': info.get('metric', ''),
                                'status_codes': info.get('status_codes', ''),
                                'path': info.get('origin_codes', '?'),
                                'locprf': info.get('locprf', ''),
                                'weight': info.get('weight', '')
                            })
            logger.info(f"Extracted BGP routes data: {extracted_data}")
        except Exception as e:
            logger.error(f"Error extracting data from BGP table: {e}")
            self.failed(f"Test failed while extracting BGP data: {e}")

class CommonCleanup(aetest.CommonCleanup):
    @aetest.subsection
    def disconnect(self):
        try:
            """Disconnect from the device."""
            device = self.parent.parameters.get('device')
            if device and device.connected:
                logger.info(f"Disconnecting from device {device.name}")
                device.disconnect()
        except Exception as e:
            logger.error(f"Error while disconnecting from the device/s: {e}")
            self.failed(f"Error while disconnecting from the device/s: {e}")

    @aetest.subsection
    def ai_summary_to_text(self):
        """
        Get the final analysys generated by the ai agent.
        If it's not 'None', save it to a text file. Otherwise do nothing, report
        and mark the cleanup as skipped.
        """

        #read ai final analysis
        final_analysis = self.parent.parameters.get('final_analysis')
        host = self.parent.parameters.get('host', "")

        if(final_analysis!= None):
            try:
                output_dir = self.parent.parameters.get('output_dir', "")

                if(output_dir):
                    summary_path = os.path.join(output_dir, f"{host}_bgp_summary.txt")
                    logger.info("AI summary written to %s", summary_path)

                    with open(summary_path, "w", encoding="utf-8") as fp:
                        ai_text="🔎 AI summary\n\n"+final_analysis
                        fp.write(ai_text)
                else:
                    logger.warning("Results path not found")
                    self.skipped("Results path not found")

            except Exception as e:
                logger.error(f"Error while saving AI response to text file: {e}")
                self.skipped(f"Error while saving AI response to text file: {e}")
        else:
            logger.error(f"AI analysis empty.")
            self.skipped(f"AI analysis empty.")
