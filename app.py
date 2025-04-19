import requests
import re
from datetime import datetime
from datetime import timedelta
from random import shuffle
import traceback
# import logging
import threading
from flask import Flask, jsonify, request, Response
from flask_caching import Cache
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from collections import OrderedDict
import os
import json
import subprocess
import semantic_version

app = Flask(__name__)

# Logging configuration
# logging.basicConfig(filename='app.log', level=print, format='%(asctime)s - %(levelname)s - %(message)s')

# Suppress only the single InsecureRequestWarning from urllib3
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

# Initialize cache
cache = Cache(app, config={"CACHE_TYPE": "simple"})

# Initialize repo vars
# repo_path = ""
# repo_retain_hours = int(os.environ.get('REPO_RETAIN_HOURS', 3))

# Initialize number of workers
num_workers = int(os.environ.get("NUM_WORKERS", 10))

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_BASE_URL = GITHUB_API_URL + "/repos/cosmos/chain-registry/contents"

# these servers have given consistent error responses, this list is used to skip them
SERVER_BLACKLIST = [
    "https://stride.api.bccnodes.com:443",
    "https://api.omniflix.nodestake.top",
    "https://cosmos-lcd.quickapi.com:443",
]

NETWORKS_NO_GOV_MODULE = [
    "noble",
    "nobletestnet",
]

# Global variables to store the data for mainnets and testnets
MAINNET_DATA = []
TESTNET_DATA = []

SEMANTIC_VERSION_PATTERN = re.compile(r"(v\d+(?:\.\d+){0,2})")


# Explicit list of chains to pull data from
def get_chain_watch_env_var():
    chain_watch = os.environ.get("CHAIN_WATCH", "")

    chain_watch.split(" ")

    if len(chain_watch) > 0:
        print(
            "CHAIN_WATCH env variable set, gathering data and watching for these chains: "
            + chain_watch
        )
    else:
        print("CHAIN_WATCH env variable not set, gathering data for all chains")

    return chain_watch


CHAIN_WATCH = get_chain_watch_env_var()


# Clone the repo
def fetch_repo():
    """Clone the GitHub repository or update it if it already exists."""
    repo_clone_url = "https://github.com/cosmos/chain-registry.git"
    repo_dir = os.path.join(os.getcwd(), "chain-registry")

    if os.path.exists(repo_dir):
        old_wd = os.getcwd()
        print(f"Repository already exists. Fetching and pulling latest changes...")
        try:
            # Navigate to the repo directory
            os.chdir(repo_dir)
            # Fetch the latest changes
            subprocess.run(["git", "fetch"], check=True)
            # Pull the latest changes
            subprocess.run(["git", "pull"], check=True)
        except subprocess.CalledProcessError:
            raise Exception("Failed to fetch and pull the latest changes.")
        finally:
            os.chdir(old_wd)
    else:
        print(f"Cloning repo {repo_clone_url}...")
        try:
            subprocess.run(["git", "clone", repo_clone_url], check=True)
        except subprocess.CalledProcessError:
            raise Exception("Failed to clone the repository.")

    return repo_dir


def get_healthy_rpc_endpoints(rpc_endpoints):
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        healthy_rpc_endpoints = [
            rpc
            for rpc, is_healthy in executor.map(
                lambda rpc: (rpc, is_rpc_endpoint_healthy(rpc["address"])), rpc_endpoints
            )
            if is_healthy
        ]

    return healthy_rpc_endpoints[:5]  # Select the first 5 healthy RPC endpoints


def get_healthy_rest_endpoints(rest_endpoints):
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        healthy_rest_endpoints = [
            rest
            for rest, is_healthy in executor.map(
                lambda rest: (rest, is_rest_endpoint_healthy(rest["address"])),
                rest_endpoints,
            )
            if is_healthy
        ]

    return healthy_rest_endpoints[:5]  # Select the first 5 healthy REST endpoints


def is_rpc_endpoint_healthy(endpoint):
    try:
        response = requests.get(f"{endpoint}/abci_info", timeout=1, verify=False)
        if response.status_code != 200:
            response = requests.get(f"{endpoint}/health", timeout=1, verify=False)
        return response.status_code == 200
    except:
        return False

def is_rest_endpoint_healthy(endpoint):
    try:
        response = requests.get(f"{endpoint}/health", timeout=1, verify=False)
        # some chains dont implement the /health endpoint. Should we just skip /health and go directly to the below?
        if response.status_code != 200:
            response = requests.get(
                f"{endpoint}/cosmos/base/tendermint/v1beta1/node_info",
                timeout=1,
                verify=False,
            )
        return response.status_code == 200
    except:
        return False

def get_latest_block_height_rpc(rpc_url):
    """Fetch the latest block height from the RPC endpoint."""
    try:
        response = requests.get(f"{rpc_url}/status", timeout=1)
        response.raise_for_status()
        data = response.json()

        if "result" in data.keys():
             data = data["result"]

        return int(
            data.get("sync_info", {}).get("latest_block_height", 0)
        )

    # RPC endpoints can return a 200 but not JSON (usually an HTML error page due to throttling or some other error)
    # Catch everything instead of just requests.RequestException
    except Exception:
        return -1  # Return -1 to indicate an error


def get_block_time_rpc(rpc_url, height, allow_retry=False):
    """Fetch the block header time for a given block height from the RPC endpoint."""
    response = None
    try:
        response = requests.get(f"{rpc_url}/block?height={height}", timeout=2)
        response.raise_for_status()
        data = response.json()

        if "result" in data.keys():
             data = data["result"]

        return data.get("block", {}).get("header", {}).get("time", "")
    # RPC endpoints can return a 200 but not JSON (usually an HTML error page due to throttling or some other error)
    # Catch everything instead of just requests.RequestException
    except Exception as e:
        print(e)
        # Attempt to retry the request if the error message indicates that the block height is too low
        # Pulls the block height from the error message and adds 20 to it
        if allow_retry and response is not None and response.status_code == 500 and response.content:
            try:
                error_message = json.loads(response.content)
                if "lowest height is " in error_message.get("error", {}).get("data", ""):
                    print("Reattempting block height request with lowest height data from previous response")
                    height = int(error_message.get("error", {}).get("data", "").split("lowest height is ")[1].strip()) + 20
                    response = requests.get(f"{rpc_url}/block?height={height}", timeout=1)
                    response.raise_for_status()
                    data = response.json()

                    if "result" in data.keys():
                        data = data["result"]

                    return data.get("block", {}).get("header", {}).get("time", "")
            except Exception as ee:
                print("Retry error:", ee)

        return None


def parse_isoformat_string(date_string):
    date_string = re.sub(r"(\.\d{6})\d+Z", r"\1Z", date_string)
    # The microseconds MUST be 6 digits long
    if "." in date_string and len(date_string.split(".")[1]) != 7 and date_string.endswith("Z"):
        micros = date_string.split(".")[-1][:-1]
        date_string = date_string.replace(micros, micros.ljust(6, "0"))
    date_string = date_string.replace("Z", "+00:00")
    return datetime.fromisoformat(date_string)


def reorder_data(data):
    ordered_data = OrderedDict(
        [
            ("type", data.get("type")),
            ("network", data.get("network")),
            ("rpc_server", data.get("rpc_server")),
            ("rest_server", data.get("rest_server")),
            ("latest_block_height", data.get("latest_block_height")),
            ("upgrade_found", data.get("upgrade_found")),
            ("upgrade_name", data.get("upgrade_name")),
            ("source", data.get("source")),
            ("upgrade_block_height", data.get("upgrade_block_height")),
            ("estimated_upgrade_time", data.get("estimated_upgrade_time")),
            ("upgrade_plan", data.get("upgrade_plan")),
            ("version", data.get("version")),
            ("error", data.get("error")),
        ]
    )
    return ordered_data


def fetch_all_endpoints(network_type, base_url, request_data):
    """Fetch all the REST and RPC endpoints for all networks and store in a map."""
    networks = (
        request_data.get("MAINNETS", [])
        if network_type == "mainnet"
        else request_data.get("TESTNETS", [])
    )
    endpoints_map = {}
    for network in networks:
        rest_endpoints, rpc_endpoints = fetch_endpoints(network, base_url)
        endpoints_map[network] = {"rest": rest_endpoints, "rpc": rpc_endpoints}
    return endpoints_map


def fetch_endpoints(network, base_url):
    """Fetch the REST and RPC endpoints for a given network."""
    try:
        response = requests.get(f"{base_url}/{network}/chain.json")
        print(f"{base_url}/{network}/chain.json")
        response.raise_for_status()
        data = response.json()
        rest_endpoints = data.get("apis", {}).get("rest", [])
        rpc_endpoints = data.get("apis", {}).get("rpc", [])
        return rest_endpoints, rpc_endpoints
    except requests.RequestException:
        return [], []

def fetch_active_upgrade_proposals(rest_url, network, network_repo_url):
    try:
        response = requests.get(
            f"{rest_url}/cosmos/gov/v1beta1/proposals?proposal_status=2", verify=False
        )

        # Handle 501 Server Error
        if response.status_code == 501:
            return None, None

        response.raise_for_status()
        data = response.json()

        for proposal in data.get("proposals", []):
            content = proposal.get("content", {})
            proposal_type = content.get("@type")
            if (
                proposal_type
                == "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal" or
                proposal_type
                == '/cosmos.upgrade.v1beta1.MsgSoftwareUpgrade'
            ):
                # Extract version from the plan name
                plan = content.get("plan", {})
                plan_name = plan.get("name", "")

                # naive regex search on whole message dump
                content_dump = json.dumps(content)

                # we tried plan_name regex match only, but the plan_name does not always track the version string
                # see Terra v5 upgrade which points to the v2.2.1 version tag
                versions = SEMANTIC_VERSION_PATTERN.findall(content_dump)
                if versions:
                    network_repo_semver_tags = get_network_repo_semver_tags(network, network_repo_url)
                    version = find_best_semver_for_versions(network, versions, network_repo_semver_tags)
                try:
                    height = int(plan.get("height", 0))
                except ValueError:
                    height = 0

                if version:
                    return plan_name, version, height
        return None, None, None
    except requests.RequestException as e:
        print(f"Error received from server {rest_url}: {e}")
        raise e
    except Exception as e:
        print(
            f"Unhandled error while requesting active upgrade endpoint from {rest_url}: {e}"
        )
        raise e

def fetch_current_upgrade_plan(rest_url, network, network_repo_url):
    try:
        response = requests.get(
            f"{rest_url}/cosmos/upgrade/v1beta1/current_plan", verify=False
        )
        response.raise_for_status()
        data = response.json()

        plan = data.get("plan", {})
        if plan:
            plan_name = plan.get("name", "")

            # Convert the plan to string and search for the version pattern
            plan_dump = json.dumps(plan)

            # Get all version matches
            version_matches = SEMANTIC_VERSION_PATTERN.findall(plan_dump)

            if version_matches:
                # Find the longest match
                network_repo_semver_tags = get_network_repo_semver_tags(network, network_repo_url)
                version = find_best_semver_for_versions(network, version_matches, network_repo_semver_tags)
                try:
                    height = int(plan.get("height", 0))
                except ValueError:
                    height = 0
                return plan_name, version, height, plan_dump

        return None, None, None, None
    except requests.RequestException as e:
        print(f"Error received from server {rest_url}: {e}")
        raise e
    except Exception as e:
        print(
            f"Unhandled error while requesting current upgrade endpoint from {rest_url}: {e}"
        )
        raise e

def fetch_network_repo_tags(network, network_repo):
    if "github.com" in network_repo:
        try:
            repo_parts = network_repo.split("/")
            repo_name = repo_parts[-1]
            repo_owner = repo_parts[-2]

            if not repo_name or not repo_owner:
                print(f"Could not parse github repo name or owner for {network}")
                return []

            tags_url = GITHUB_API_URL + f"/repos/{repo_owner}/{repo_name}/tags"
            tags = requests.get(tags_url)
            return list(map(lambda tag: tag["name"], tags.json()))
        except Exception as e:
            print(f"Could not fetch tags from github for network {network}")
            print(e)
            return []
    else:
        print(f"Could not fetch tags from github for network {network}: unsupported repo url {network_repo}")
        return []

def get_network_repo_semver_tags(network, network_repo_url):
    cached_tags = cache.get(network_repo_url + "_tags")
    if not cached_tags:
        network_repo_tag_strings = fetch_network_repo_tags(network, network_repo_url)
        #cache response from network repo url to reduce api calls to whatever service is hosting the repo
        cache.set(network_repo_url + "_tags", network_repo_tag_strings, timeout=600)
    else:
        network_repo_tag_strings = cached_tags

    network_repo_semver_tags = []
    for tag in network_repo_tag_strings:
        #only use semantic version tags
        try:
            if tag.startswith("v"):
                version = semantic_version.Version(tag[1:])
            else:
                version = semantic_version.Version(tag)
            network_repo_semver_tags.append(version)
        except Exception as e:
            pass

    return network_repo_semver_tags

def find_best_semver_for_versions(network, network_version_strings, network_repo_semver_tags):
    if len(network_repo_semver_tags) == 0:
        return max(network_version_strings, key=len)

    try:
        # find version matches in the repo tags
        possible_semvers = []
        for version_string in network_version_strings:
            if version_string.startswith("v"):
                version_string = version_string[1:]

            contains_minor_version = True
            contains_patch_version = True

            # our regex captures version strings like "v1" without a minor or patch version, so we need to check for that
            # are these conditions good enough or is it missing any cases?
            if "." not in version_string:
                contains_minor_version = False
                contains_patch_version = False
                version_string = version_string + ".0.0"
            elif version_string.count(".") == 1:
                contains_patch_version = False
                version_string = version_string + ".0"

            current_semver = semantic_version.Version(version_string)

            for semver_tag in network_repo_semver_tags:
                # find matching tags based on what information we have
                if semver_tag.major == current_semver.major:
                    if contains_minor_version:
                        if semver_tag.minor == current_semver.minor:
                            if contains_patch_version:
                                if semver_tag.patch == current_semver.patch:
                                    possible_semvers.append(semver_tag)
                            else:
                                possible_semvers.append(semver_tag)
                    else:
                        possible_semvers.append(semver_tag)

        # currently just return the highest semver from the list of possible matches. This may be too naive
        if len(possible_semvers) != 0:
            #sorting is built into the semantic version library
            possible_semvers.sort(reverse=True)
            semver = possible_semvers[0]
            return f"v{semver.major}.{semver.minor}.{semver.patch}"
    except Exception as e:
        print(f"Failed to parse version strings into semvers for network {network}")
        print(e)
        return max(network_version_strings, key=len)

    return max(network_version_strings, key=len)

def fetch_data_for_networks_wrapper(network, network_type, repo_path):
    """Wrapper function for fetching data for a given network. Prints the chain name that is erroring out for better visibility"""
    try:
        return fetch_data_for_network(network, network_type, repo_path)
    except Exception as e:
        print(f"Error fetching data for network {network}: {e}")
        raise e

def fetch_data_for_network(network, network_type, repo_path):
    """Fetch data for a given network."""

    # Construct the path to the chain.json file based on network type
    if network_type == "mainnet":
        chain_json_path = os.path.join(repo_path, network, "chain.json")
    elif network_type == "testnet":
        chain_json_path = os.path.join(repo_path, "testnets", network, "chain.json")
    else:
        raise ValueError(f"Invalid network type: {network_type}")
    output_data = {}
    err_output_data = {
        "network": network,
        "type": network_type,
        "error": "insufficient data in Cosmos chain registry, consider a PR to cosmos/chain-registry",
        "upgrade_found": False,
    }

    # Check if the chain.json file exists
    if not os.path.exists(chain_json_path):
        print(f"chain.json not found for network {network}. Skipping...")
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, chain.json not found for {network}. Consider a PR to cosmos/chain-registry"
        return err_output_data

    # Load the chain.json data
    with open(chain_json_path, "r") as file:
        data = json.load(file)

    network_repo_url = data.get("codebase", {}).get("git_repo", None)

    rest_endpoints = data.get("apis", {}).get("rest", [])
    rpc_endpoints = data.get("apis", {}).get("rpc", [])

    # Prioritize RPC endpoints for fetching the latest block height
    latest_block_height = -1
    healthy_rpc_endpoints = get_healthy_rpc_endpoints(rpc_endpoints)
    healthy_rest_endpoints = get_healthy_rest_endpoints(rest_endpoints)

    if len(healthy_rpc_endpoints) == 0:
        print(
            f"No healthy RPC endpoints found for network {network} while searching through {len(rpc_endpoints)} endpoints. Skipping..."
        )
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, no healthy RPC servers for {network}. Consider a PR to cosmos/chain-registry"
        return err_output_data

    # Shuffle the healthy endpoints
    shuffle(healthy_rpc_endpoints)
    shuffle(healthy_rest_endpoints)

    rpc_server_used = ""
    for rpc_endpoint in healthy_rpc_endpoints:
        if isinstance(rpc_endpoint, dict):
            current_endpoint = rpc_endpoint.get("address", None)
            latest_block_height = get_latest_block_height_rpc(current_endpoint)
            if latest_block_height > 0:
                rpc_server_used = current_endpoint
                break

    if latest_block_height < 0:
        print(
            f"No RPC endpoints returned latest height for network {network} while searching through {len(rpc_endpoints)} endpoints. Skipping..."
        )
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, no RPC servers returned latest block height for {network}. Consider a PR to cosmos/chain-registry"
        return err_output_data

    if len(healthy_rest_endpoints) == 0:
        print(
            f"No healthy REST endpoints found for network {network} while searching through {len(rest_endpoints)} endpoints. Skipping..."
        )
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, no healthy REST servers for {network}. Consider a PR to cosmos/chain-registry"
        err_output_data["latest_block_height"] = latest_block_height
        err_output_data["rpc_server"] = rpc_server_used
        return err_output_data

    print(
        f"Found {len(healthy_rest_endpoints)} rest endpoints and {len(healthy_rpc_endpoints)} rpc endpoints for {network}"
    )

    # Check for active upgrade proposals
    upgrade_block_height = None
    upgrade_name = ""
    upgrade_version = ""
    source = ""
    rest_server_used = ""

    for index, rest_endpoint in healthy_rest_endpoints:
        if isinstance(rest_endpoint, dict):
            current_endpoint = rest_endpoint.get("address", None)

            if current_endpoint in SERVER_BLACKLIST:
                continue

            active_upgrade_check_failed = False
            upgrade_plan_check_failed = False
            try:
                if network in NETWORKS_NO_GOV_MODULE:
                    raise Exception("Network does not have gov module")
                (
                    active_upgrade_name,
                    active_upgrade_version,
                    active_upgrade_height,
                ) = fetch_active_upgrade_proposals(current_endpoint, network, network_repo_url)

            except:
                (
                    active_upgrade_name,
                    active_upgrade_version,
                    active_upgrade_height,
                ) = (None, None, None)
                active_upgrade_check_failed = True

            try:
                (
                    current_upgrade_name,
                    current_upgrade_version,
                    current_upgrade_height,
                    current_plan_dump,
                ) = fetch_current_upgrade_plan(current_endpoint, network, network_repo_url)
            except:
                (
                    current_upgrade_name,
                    current_upgrade_version,
                    current_upgrade_height,
                    current_plan_dump,
                ) = (None, None, None, None)
                upgrade_plan_check_failed = True

            if active_upgrade_check_failed and upgrade_plan_check_failed:
                if index + 1 < len(healthy_rest_endpoints):
                    print(
                        f"Failed to query rest endpoints {current_endpoint}, trying next rest endpoint"
                    )
                    continue
                else:
                    print(
                        f"Failed to query rest endpoints {current_endpoint}, all out of endpoints to try"
                    )
                    break

            if active_upgrade_check_failed and network not in NETWORKS_NO_GOV_MODULE:
                print(
                    f"Failed to query active upgrade endpoint {current_endpoint}, trying next rest endpoint"
                )
                continue

            if (
                active_upgrade_version
                and (active_upgrade_height is not None)
                and active_upgrade_height > latest_block_height
            ):
                upgrade_block_height = active_upgrade_height
                upgrade_version = active_upgrade_version
                upgrade_name = active_upgrade_name
                source = "active_upgrade_proposals"
                rest_server_used = current_endpoint
                break

            if (
                current_upgrade_version
                and (current_upgrade_height is not None)
                and (current_plan_dump is not None)
                and current_upgrade_height > latest_block_height
            ):
                upgrade_block_height = current_upgrade_height
                upgrade_plan = json.loads(current_plan_dump)
                upgrade_version = current_upgrade_version
                upgrade_name = current_upgrade_name
                source = "current_upgrade_plan"
                rest_server_used = current_endpoint
                # Extract the relevant information from the parsed JSON
                info = {}
                binaries = []
                try:
                    info = json.loads(upgrade_plan.get("info", "{}"))
                    binaries = info.get("binaries", {})
                except:
                    print(f"Failed to parse binaries for network {network}. Non-fatal error, skipping...")
                    pass

                plan_height = upgrade_plan.get("height", -1)
                try:
                    plan_height = int(plan_height)
                except ValueError:
                    plan_height = -1

                # Include the expanded information in the output data
                output_data["upgrade_plan"] = {
                    "height": plan_height,
                    "binaries": binaries,
                    "name": upgrade_plan.get("name", None),
                    "upgraded_client_state": upgrade_plan.get("upgraded_client_state", None),
                }
                break

            if not active_upgrade_version and not current_upgrade_version:
                # this is where the "no upgrades found block runs"
                rest_server_used = current_endpoint
                break

    current_block_time = None
    past_block_time = None
    avg_block_time_seconds = None
    for rpc_endpoint in healthy_rpc_endpoints:
        if isinstance(rpc_endpoint, dict):
            current_endpoint = rpc_endpoint.get("address", None)
            current_block_time = get_block_time_rpc(current_endpoint, latest_block_height)
            past_block_time = get_block_time_rpc(current_endpoint, latest_block_height - 10000, allow_retry=True)

            if current_block_time and past_block_time:
                break
            else:
                print(
                    f"Failed to query current and past block time for rpc endpoint {current_endpoint}, trying next rpc endpoint"
                )
                continue

    if current_block_time and past_block_time:
        current_block_datetime = parse_isoformat_string(current_block_time)
        past_block_datetime = parse_isoformat_string(past_block_time)
        avg_block_time_seconds = (
            current_block_datetime - past_block_datetime
        ).total_seconds() / 10000

    # Estimate the upgrade time
    estimated_upgrade_time = None
    if upgrade_block_height and avg_block_time_seconds:
        estimated_seconds_until_upgrade = avg_block_time_seconds * (
            upgrade_block_height - latest_block_height
        )
        estimated_upgrade_datetime = datetime.utcnow() + timedelta(
            seconds=estimated_seconds_until_upgrade
        )
        estimated_upgrade_time = estimated_upgrade_datetime.isoformat().replace(
            "+00:00", "Z"
        )

    # Add logo_URIs to the output data if available, otherwise set to 'no logo'
    # Reorder explorers to show 'mintscan' first, then 'ping.pub', then the rest
    explorers = data.get("explorers", [])
    if explorers != "no explorers":
        explorers.sort(key=lambda x: (x.get("kind") != "mintscan", x.get("kind") != "ping.pub"))
    else:
        explorers = "no explorers"

    # Ensure upgrade_found is null when rpc_server is null
    if not rpc_server_used:
        output_data["upgrade_found"] = None
        output_data["upgrade_name"] = None
        output_data["source"] = None
        output_data["upgrade_block_height"] = None
        output_data["estimated_upgrade_time"] = None
        output_data["upgrade_plan"] = None
        output_data["version"] = None

    output_data = {
        "network": network,
        "type": network_type,
        "rpc_server": rpc_server_used,
        "rest_server": rest_server_used,
        "latest_block_height": latest_block_height,
        "upgrade_found": upgrade_version != "",
        "upgrade_name": upgrade_name,
        "source": source,
        "upgrade_block_height": upgrade_block_height,
        "upgrade_plan": output_data.get("upgrade_plan", None),
        "estimated_upgrade_time": estimated_upgrade_time,
        "version": upgrade_version,
        "logo_URIs": data.get("logo_URIs", "no logo"),
        "explorers": explorers
    }
    print(f"Completed fetch data for network {network}")
    return output_data


# periodic cache update
def update_data():
    """Function to periodically update the data for mainnets and testnets."""

    while True:
        start_time = datetime.now()  # Capture the start time
        print("Starting data update cycle...")

        # Git clone or fetch & pull
        try:
            repo_path = fetch_repo()
            print(f"Repo path: {repo_path}")
        except Exception as e:
            print(f"Error downloading and extracting repo: {e}")
            print("Error encountered. Sleeping for 5 seconds before retrying...")
            sleep(5)
            continue

        try:
            # Process mainnets & testnets
            mainnet_networks = [
                d
                for d in os.listdir(repo_path)
                if os.path.isdir(os.path.join(repo_path, d))
                and not d.startswith((".", "_"))
                and d != "testnets"
            ]

            if len(CHAIN_WATCH) != 0:
                mainnet_networks = [d for d in mainnet_networks if d in CHAIN_WATCH]

            testnet_path = os.path.join(repo_path, "testnets")
            testnet_networks = [
                d
                for d in os.listdir(testnet_path)
                if os.path.isdir(os.path.join(testnet_path, d))
                and not d.startswith((".", "_"))
            ]

            if len(CHAIN_WATCH) != 0:
                testnet_networks = [d for d in testnet_networks if d in CHAIN_WATCH]

            with ThreadPoolExecutor() as executor:
                testnet_data = list(
                    filter(
                        None,
                        executor.map(
                            lambda network, path: fetch_data_for_networks_wrapper(
                                network, "testnet", path
                            ),
                            testnet_networks,
                            [repo_path] * len(testnet_networks),
                        ),
                    )
                )
                mainnet_data = list(
                    filter(
                        None,
                        executor.map(
                            lambda network, path: fetch_data_for_networks_wrapper(
                                network, "mainnet", path
                            ),
                            mainnet_networks,
                            [repo_path] * len(mainnet_networks),
                        ),
                    )
                )

            # Update the Flask cache
            cache.set("MAINNET_DATA", mainnet_data)
            cache.set("TESTNET_DATA", testnet_data)

            elapsed_time = (
                datetime.now() - start_time
            ).total_seconds()  # Calculate the elapsed time
            print(
                f"Data update cycle completed in {elapsed_time} seconds. Sleeping for 1 minute..."
            )
            sleep(60)
        except Exception as e:
            elapsed_time = (
                datetime.now() - start_time
            ).total_seconds()  # Calculate the elapsed time in case of an error
            traceback.print_exc()
            print(f"Error in update_data loop after {elapsed_time} seconds: {e}")
            print("Error encountered. Sleeping for 1 minute before retrying...")
            sleep(60)


def start_update_data_thread():
    update_thread = threading.Thread(target=update_data)
    update_thread.daemon = True
    update_thread.start()


@app.route("/healthz")
def health_check():
    return jsonify(status="OK"), 200

@app.route("/mainnets")
# @cache.cached(timeout=600)  # Cache the result for 10 minutes
def get_mainnet_data():
    results = cache.get("MAINNET_DATA")
    if results is None:
        return jsonify({"error": "Data not available"}), 500

    results = [r for r in results if r is not None]
    sorted_results = sorted(results, key=lambda x: x["upgrade_found"], reverse=True)
    reordered_results = [reorder_data(result) for result in sorted_results]
    return Response(
        json.dumps(reordered_results) + "\n", content_type="application/json"
    )


@app.route("/testnets")
# @cache.cached(timeout=600)  # Cache the result for 10 minutes
def get_testnet_data():
    results = cache.get("TESTNET_DATA")
    if results is None:
        return jsonify({"error": "Data not available"}), 500

    results = [r for r in results if r is not None]
    sorted_results = sorted(results, key=lambda x: x["upgrade_found"], reverse=True)
    reordered_results = [reorder_data(result) for result in sorted_results]
    return Response(
        json.dumps(reordered_results) + "\n", content_type="application/json"
    )


if __name__ == "__main__":
    app.debug = True
    start_update_data_thread()
    app.run(host="0.0.0.0", use_reloader=False)
