import requests
import re
from datetime import datetime
from datetime import timedelta
from random import shuffle
import traceback
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
import logging
import sys
from loguru import logger
from dotenv import load_dotenv, find_dotenv
import base64

# Load environment variables from .env file explicitly
load_dotenv(find_dotenv(), override=True)

# --- Configuration Loading ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
NETWORK_BLACKLIST_CSV = os.environ.get("NETWORK_BLACKLIST", "")
NETWORK_BLACKLIST = [net.strip() for net in NETWORK_BLACKLIST_CSV.split(",") if net.strip()]
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", 20))
CHAIN_REGISTRY_REPO_URL = os.environ.get("CHAIN_REGISTRY_REPO_URL", "https://github.com/cosmos/chain-registry.git")
CHAIN_REGISTRY_DIR_NAME = os.environ.get("CHAIN_REGISTRY_DIR_NAME", "chain-registry")
GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
SERVER_BLACKLIST_CSV = os.environ.get("SERVER_BLACKLIST", "https://stride.api.bccnodes.com:443,https://api.omniflix.nodestake.top,https://cosmos-lcd.quickapi.com:443,https://osmosis.rpc.stakin-nodes.com:443")
SERVER_BLACKLIST = [srv.strip() for srv in SERVER_BLACKLIST_CSV.split(",") if srv.strip()]
NETWORKS_NO_GOV_MODULE_CSV = os.environ.get("NETWORKS_NO_GOV_MODULE_CSV", "noble,nobletestnet")
NETWORKS_NO_GOV_MODULE = [net.strip() for net in NETWORKS_NO_GOV_MODULE_CSV.split(",") if net.strip()]
COSMWASM_GOV_CONFIG_JSON = os.environ.get("COSMWASM_GOV_CONFIG_JSON", '{"neutron": {"contract_address": "neutron1suhgf5svhu4usrurvxzlgn54ksxmn8gljarjtxqnapv8kjnp4nrs7d743d", "query_type": "list_proposals"}}')
try:
    COSMWASM_GOV_CONFIG = json.loads(COSMWASM_GOV_CONFIG_JSON)
except json.JSONDecodeError:
    logger.error("Failed to parse COSMWASM_GOV_CONFIG_JSON. Using empty config.", error=COSMWASM_GOV_CONFIG_JSON)
    COSMWASM_GOV_CONFIG = {}
PREFERRED_EXPLORERS_CSV = os.environ.get("PREFERRED_EXPLORERS_CSV", "ping.pub,mintscan.io,nodes.guru")
PREFERRED_EXPLORERS = [exp.strip() for exp in PREFERRED_EXPLORERS_CSV.split(",") if exp.strip()]
UPDATE_INTERVAL_SECONDS = int(os.environ.get("UPDATE_INTERVAL_SECONDS", 60))
HEALTH_CHECK_TIMEOUT_SECONDS = int(os.environ.get("HEALTH_CHECK_TIMEOUT_SECONDS", 1))
BLOCK_FETCH_TIMEOUT_SECONDS = int(os.environ.get("BLOCK_FETCH_TIMEOUT_SECONDS", 2))
STATUS_TIMEOUT_SECONDS = int(os.environ.get("STATUS_TIMEOUT_SECONDS", 1)) # Timeout for /status endpoint
COSMWASM_TIMEOUT_SECONDS = int(os.environ.get("COSMWASM_TIMEOUT_SECONDS", 10))
MAX_HEALTHY_ENDPOINTS = int(os.environ.get("MAX_HEALTHY_ENDPOINTS", 5))
BLOCK_RANGE_FOR_AVG_TIME = int(os.environ.get("BLOCK_RANGE_FOR_AVG_TIME", 10000))
TAG_CACHE_TIMEOUT_SECONDS = int(os.environ.get("TAG_CACHE_TIMEOUT_SECONDS", 3600))
FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5001))
BLOCK_TIME_RETRIES = int(os.environ.get("BLOCK_TIME_RETRIES", 3))
EXPLORER_HEALTH_TIMEOUT_SECONDS = int(os.environ.get("EXPLORER_HEALTH_TIMEOUT_SECONDS", 2))
# --- End Configuration Loading ---

app = Flask(__name__)

# Set log level based on environment variable (already loaded)
logger.remove()
logger = logger.bind(network="GLOBAL")

# Define log formats
debug_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[network]}</cyan> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
info_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[network]}</cyan> - <level>{message}</level>"

# Add handler based on LOG_LEVEL
if LOG_LEVEL == "TRACE":
    logger.add(sys.stderr, format=debug_format, colorize=True, level=LOG_LEVEL)
elif LOG_LEVEL == "DEBUG":
    logger.add(sys.stderr, format=debug_format, colorize=True, level=LOG_LEVEL)
else:
    actual_log_level = "INFO"
    logger.add(sys.stderr, format=info_format, colorize=True, level=actual_log_level)
    LOG_LEVEL = actual_log_level

# Log the configured level
logger.info(f"Logging configured at level: {LOG_LEVEL}")
logger.debug(f"Using Chain Registry Repo: {CHAIN_REGISTRY_REPO_URL}")
logger.debug(f"Using Chain Registry Dir: {CHAIN_REGISTRY_DIR_NAME}")
logger.debug(f"Update Interval: {UPDATE_INTERVAL_SECONDS}s")
logger.debug(f"Max Healthy Endpoints: {MAX_HEALTHY_ENDPOINTS}")
logger.debug(f"Flask Host: {FLASK_HOST}")
logger.debug(f"Flask Port: {FLASK_PORT}")

# Suppress only the single InsecureRequestWarning from urllib3
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

# Initialize cache
cache = Cache(app, config={"CACHE_TYPE": "simple"})

# Global variables to store the data for mainnets and testnets
MAINNET_DATA = []
TESTNET_DATA = []
CHAIN_WATCH = []

SEMANTIC_VERSION_PATTERN = re.compile(r"(v\d+(?:\.\d+){0,2})")

def get_chain_watch_env_var():
    chain_watch_str = os.environ.get("CHAIN_WATCH", "")
    chain_watch_list = [chain.strip() for chain in chain_watch_str.split(",") if chain.strip()]

    if len(chain_watch_list) > 0:
        logger.info(
            f"CHAIN_WATCH env variable set. Watching: {', '.join(chain_watch_list)}",
        )
    else:
        logger.info("CHAIN_WATCH env variable not set, gathering data for all chains")

    return chain_watch_list


# Clone the repo
def fetch_repo():
    """Clone or update the chain registry repository."""
    repo_clone_url = CHAIN_REGISTRY_REPO_URL
    repo_dir = os.path.join(os.getcwd(), CHAIN_REGISTRY_DIR_NAME)
    try:
        if os.path.exists(repo_dir):
            subprocess.run(["git", "-C", repo_dir, "pull"], check=True)
        else:
            subprocess.run(["git", "clone", repo_clone_url, repo_dir], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to fetch the repository", error=str(e))
        raise Exception(f"Failed to fetch the repository: {e}")
    return repo_dir


def get_healthy_rpc_endpoints(rpc_endpoints):
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        healthy_rpc_endpoints = [
            rpc
            for rpc, is_healthy in executor.map(
                lambda rpc: (rpc, is_rpc_endpoint_healthy(rpc["address"])), rpc_endpoints
            )
            if is_healthy
        ]

    return healthy_rpc_endpoints[:MAX_HEALTHY_ENDPOINTS]


def get_healthy_rest_endpoints(rest_endpoints):
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        healthy_rest_endpoints = [
            rest
            for rest, is_healthy in executor.map(
                lambda rest: (rest, is_rest_endpoint_healthy(rest["address"])),
                rest_endpoints,
            )
            if is_healthy
        ]

    return healthy_rest_endpoints[:MAX_HEALTHY_ENDPOINTS]


def is_rpc_endpoint_healthy(endpoint):
    try:
        response = requests.get(f"{endpoint}/abci_info", timeout=HEALTH_CHECK_TIMEOUT_SECONDS, verify=False)
        if response.status_code != 200:
            response = requests.get(f"{endpoint}/health", timeout=HEALTH_CHECK_TIMEOUT_SECONDS, verify=False)
        return response.status_code == 200
    except:
        return False

def is_rest_endpoint_healthy(endpoint):
    try:
        response = requests.get(f"{endpoint}/health", timeout=HEALTH_CHECK_TIMEOUT_SECONDS, verify=False)
        if response.status_code != 200:
            response = requests.get(
                f"{endpoint}/cosmos/base/tendermint/v1beta1/node_info",
                timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
                verify=False,
            )
        return response.status_code == 200
    except:
        return False

def get_latest_block_height_rpc(rpc_url):
    """Fetch the latest block height from the RPC endpoint."""
    try:
        response = requests.get(f"{rpc_url}/status", timeout=STATUS_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()

        if "result" in data.keys():
             data = data["result"]

        return int(
            data.get("sync_info", {}).get("latest_block_height", 0)
        )
    except Exception:
        return -1


def get_block_time_rpc(rpc_url, height, retries=None, network=None):
    """Fetch the block header time for a given block height from the RPC endpoint."""
    effective_retries = retries if retries is not None else BLOCK_TIME_RETRIES
    network_logger = logger.bind(network=network.upper() if network else "UNKNOWN")
    for attempt in range(effective_retries):
        try:
            response = requests.get(f"{rpc_url}/block?height={height}", timeout=BLOCK_FETCH_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            if "result" in data.keys():
                data = data["result"]
            return data.get("block", {}).get("header", {}).get("time", "")
        except requests.exceptions.HTTPError as e:
            network_logger.trace(f"HTTP error on attempt {attempt + 1}: {str(e)}")
            if attempt == effective_retries - 1:
                return None
        except Exception as e:
            network_logger.trace(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt == effective_retries - 1:
                return None
        sleep(2 ** attempt)

def estimate_upgrade_time(latest_block_time, past_block_time, latest_block_height, upgrade_block_height):
    """Estimate the upgrade time based on block times and heights."""
    if not latest_block_time or not past_block_time or upgrade_block_height is None:
        return None

    latest_block_datetime = parse_isoformat_string(latest_block_time)
    past_block_datetime = parse_isoformat_string(past_block_time)

    avg_block_time_seconds = (latest_block_datetime - past_block_datetime).total_seconds() / BLOCK_RANGE_FOR_AVG_TIME

    blocks_until_upgrade = upgrade_block_height - latest_block_height
    estimated_seconds_until_upgrade = avg_block_time_seconds * blocks_until_upgrade
    estimated_upgrade_datetime = datetime.utcnow() + timedelta(seconds=estimated_seconds_until_upgrade)

    return estimated_upgrade_datetime.isoformat().replace("+00:00", "Z")

def parse_isoformat_string(date_string):
    date_string = re.sub(r"(\.\d{6})\d+Z", r"\1Z", date_string)
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
        logger.info("Fetching endpoints", url=f"{base_url}/{network}/chain.json")
        response.raise_for_status()
        data = response.json()
        rest_endpoints = data.get("apis", {}).get("rest", [])
        rpc_endpoints = data.get("apis", {}).get("rpc", [])
        return rest_endpoints, rpc_endpoints
    except requests.RequestException as e:
        logger.error("Error fetching endpoints", error=str(e))
        return [], []
    
def fetch_active_upgrade_proposals(rest_url, network, network_repo_url):
    network_logger = logger.bind(network=network.upper())
    try:
        [plan_name, version, height] = fetch_active_upgrade_proposals_v1(rest_url, network, network_repo_url)
    except RequiresGovV1Exception as e:
        [plan_name, version, height] = fetch_active_upgrade_proposals_v1(rest_url, network, network_repo_url)
    except Exception as e:
        raise e
    
    return plan_name, version, height


def fetch_active_upgrade_proposals_v1beta1(rest_url, network, network_repo_url):
    network_logger = logger.bind(network=network.upper())
    try:
        response = requests.get(
            f"{rest_url}/cosmos/gov/v1beta1/proposals?proposal_status=2", verify=False
        )

        if response.status_code == 501:
            return None, None

        if response.status_code != 200:
            response_json = {}
            try:
                response_json = response.json()
            except:
                pass
            if "message" in response_json and "can't convert" in response_json["message"]:
                raise RequiresGovV1Exception("gov v1 is required")

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
                plan = content.get("plan", {})
                plan_name = plan.get("name", "")

                content_dump = json.dumps(content)

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
        status_code = e.response.status_code if e.response is not None else "N/A"
        network_logger.trace(
            "RequestException during v1beta1 active proposal fetch",
            server=rest_url,
            status_code=status_code,
            error=str(e),
        )
        raise e
    except RequiresGovV1Exception as e:
        network_logger.debug("RequiresGovV1Exception caught, will try v1 endpoint", server=rest_url)
        raise e
    except Exception as e:
        network_logger.error(
            f"Unhandled error while requesting v1beta1 active upgrade endpoint",
            server=rest_url,
            error=str(e),
            error_type=type(e).__name__,
            trace=traceback.format_exc()
        )
        raise e
    
def fetch_active_upgrade_proposals_v1(rest_url, network, network_repo_url):
    network_logger = logger.bind(network=network.upper())
    proposal_statuses_to_check = ["2", "3"]

    for status_code in proposal_statuses_to_check:
        network_logger.trace(f"Querying for proposals with status {status_code}")
        all_proposals = []
        next_key = None

        while True:
            try:
                query_params = {"proposal_status": status_code}
                if next_key:
                    query_params["pagination.key"] = next_key

                response = requests.get(
                    f"{rest_url}/cosmos/gov/v1/proposals", params=query_params, verify=False
                )

                if response.status_code == 501:
                    network_logger.trace(f"Gov v1 endpoint not implemented (501) for status {status_code}")
                    all_proposals = []
                    break

                response.raise_for_status()
                data = response.json()
                network_logger.trace(f"Raw active proposals v1 data (status {status_code}, page key: {next_key})", data=str(data)[:1000])

                page_proposals = data.get("proposals", [])
                if page_proposals:
                    all_proposals.extend(page_proposals)

                next_key = data.get("pagination", {}).get("next_key")
                if not next_key:
                    network_logger.trace(f"No more pages for status {status_code}")
                    break

                network_logger.trace(f"Found next page key for status {status_code}: {next_key}")
                sleep(0.2)

            except requests.RequestException as e:
                status_code_http = e.response.status_code if e.response is not None else "N/A"
                network_logger.trace(
                    f"RequestException during v1 active proposal fetch pagination (status {status_code})",
                    server=rest_url,
                    status_code=status_code_http,
                    error=str(e),
                )
                all_proposals = []
                break
            except Exception as e:
                network_logger.error(
                    f"Unhandled error during v1 active proposal fetch pagination (status {status_code})",
                    server=rest_url,
                    error=str(e),
                    error_type=type(e).__name__,
                    trace=traceback.format_exc()
                )
                all_proposals = []
                break

        network_logger.trace(f"Processing {len(all_proposals)} proposals found for status {status_code}")
        for proposal in all_proposals:
            messages = proposal.get("messages", [])
            proposal_id = proposal.get("id", "N/A")

            for message in messages:
                proposal_type = message.get("@type")
                upgrade_content = None

                if (
                    proposal_type == "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal" or
                    proposal_type == '/cosmos.upgrade.v1beta1.MsgSoftwareUpgrade'
                ):
                    network_logger.trace(f"Found direct upgrade message in proposal {proposal_id} (status {status_code})", message_data=message)
                    upgrade_content = message

                elif proposal_type == "/cosmos.gov.v1.MsgExecLegacyContent":
                    network_logger.trace(f"Found MsgExecLegacyContent in proposal {proposal_id} (status {status_code}), checking nested content...")
                    legacy_content = message.get("content")
                    if legacy_content and legacy_content.get("@type") == "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal":
                         network_logger.trace(f"Found nested SoftwareUpgradeProposal in proposal {proposal_id} (status {status_code})", message_data=legacy_content)
                         upgrade_content = legacy_content
                    else:
                         network_logger.trace(f"Nested content is not a SoftwareUpgradeProposal in proposal {proposal_id} (status {status_code})")

                if upgrade_content:
                    plan = upgrade_content.get("plan", {})
                    plan_name = plan.get("name", "")

                    content_dump = json.dumps(upgrade_content)
                    network_logger.trace(f"Content dump for version search (prop {proposal_id}, status {status_code})", content=content_dump)

                    versions = SEMANTIC_VERSION_PATTERN.findall(content_dump)
                    if not versions and plan_name:
                         versions.extend(SEMANTIC_VERSION_PATTERN.findall(plan_name))

                    network_logger.trace(f"Regex version matches found (prop {proposal_id}, status {status_code})", matches=versions)

                    version = None
                    if versions:
                        unique_versions = list(set(versions))
                        network_logger.trace(f"Unique version strings found: {unique_versions}")
                        network_repo_semver_tags = get_network_repo_semver_tags(network, network_repo_url)
                        network_logger.trace(f"Repo tags for semver check (prop {proposal_id}, status {status_code})", tags=network_repo_semver_tags)
                        version = find_best_semver_for_versions(network, unique_versions, network_repo_semver_tags)
                        network_logger.trace(f"Best semver found (prop {proposal_id}, status {status_code})", best_version=version)
                    else:
                        network_logger.trace(f"No regex version matches in content dump or plan name (prop {proposal_id}, status {status_code})")

                    try:
                        height = int(plan.get("height", 0))
                    except ValueError:
                        height = 0
                        network_logger.trace(f"Could not parse height from plan (prop {proposal_id}, status {status_code})", plan_height=plan.get("height"))

                    if version:
                        network_logger.trace(f"Returning valid upgrade found in proposal {proposal_id} (status {status_code})", name=plan_name, version=version, height=height)
                        return plan_name, version, height
                    else:
                        network_logger.trace(f"Version extraction failed for proposal {proposal_id} (status {status_code}), continuing search...")

        network_logger.trace(f"No suitable active upgrade proposal found after checking all {len(all_proposals)} proposals for status {status_code}")


    network_logger.trace("No suitable active upgrade proposal found after checking all specified statuses")
    return None, None, None

def fetch_current_upgrade_plan(rest_url, network, network_repo_url):
    network_logger = logger.bind(network=network.upper())
    try:
        response = requests.get(
            f"{rest_url}/cosmos/upgrade/v1beta1/current_plan", verify=False
        )
        response.raise_for_status()
        data = response.json()
        network_logger.trace("Raw current plan data", data=data)

        plan = data.get("plan", {})
        if plan:
            plan_name = plan.get("name", "")
            network_logger.trace("Found plan in current_plan endpoint", plan_data=plan)

            plan_dump = json.dumps(plan)
            network_logger.trace("Plan dump for version search", content=plan_dump)

            version_matches = SEMANTIC_VERSION_PATTERN.findall(plan_dump)
            network_logger.trace("Regex version matches found in plan", matches=version_matches)

            version = None
            if version_matches:
                network_repo_semver_tags = get_network_repo_semver_tags(network, network_repo_url)
                network_logger.trace("Repo tags for semver check (plan)", tags=network_repo_semver_tags)
                version = find_best_semver_for_versions(network, version_matches, network_repo_semver_tags)
                network_logger.trace("Best semver found (plan)", best_version=version)
            else:
                network_logger.trace("No regex version matches in plan dump")

            try:
                height = int(plan.get("height", 0))
            except ValueError:
                height = 0
                network_logger.trace("Could not parse height from plan", plan_height=plan.get("height"))

            if version:
                 network_logger.trace("Returning valid upgrade found in plan", name=plan_name, version=version, height=height)
                 return plan_name, version, height, plan_dump
            else:
                 network_logger.trace("Version extraction failed for plan, returning None")

        else:
            network_logger.trace("No plan found in current_plan response")

        return None, None, None, None
    except requests.RequestException as e:
        status_code = e.response.status_code if e.response is not None else "N/A"
        network_logger.trace(
            "RequestException during current plan fetch",
            server=rest_url,
            status_code=status_code,
            error=str(e),
        )
        raise e
    except Exception as e:
        network_logger.error(
            "Unhandled error while requesting current upgrade endpoint",
            server=rest_url,
            error=str(e),
            error_type=type(e).__name__,
            trace=traceback.format_exc()
        )
        raise e

def fetch_network_repo_tags(network, network_repo):
    if "github.com" in network_repo:
        try:
            repo_parts = network_repo.split("/")
            repo_name = repo_parts[-1]
            repo_owner = repo_parts[-2]
            tags_url = f"{GITHUB_API_URL}/repos/{repo_owner}/{repo_name}/tags"
            tags = []
            while tags_url:
                response = requests.get(tags_url)
                response.raise_for_status()
                tags.extend(response.json())
                tags_url = response.links.get("next", {}).get("url")
            return [tag["name"] for tag in tags]
        except Exception as e:
            logger.debug("Error fetching tags", network=network, error=str(e))
            return []
    return []

def get_network_repo_semver_tags(network, network_repo_url):
    cached_tags = cache.get(network_repo_url + "_tags")
    if not cached_tags:
        network_repo_tag_strings = fetch_network_repo_tags(network, network_repo_url)
        cache.set(network_repo_url + "_tags", network_repo_tag_strings, timeout=TAG_CACHE_TIMEOUT_SECONDS)
    else:
        network_repo_tag_strings = cached_tags

    network_repo_semver_tags = []
    for tag in network_repo_tag_strings:
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
        possible_semvers = []
        for version_string in network_version_strings:
            if version_string.startswith("v"):
                version_string = version_string[1:]

            contains_minor_version = True
            contains_patch_version = True

            if "." not in version_string:
                contains_minor_version = False
                contains_patch_version = False
                version_string = version_string + ".0.0"
            elif version_string.count(".") == 1:
                contains_patch_version = False
                version_string = version_string + ".0"

            current_semver = semantic_version.Version(version_string)

            for semver_tag in network_repo_semver_tags:
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

        if len(possible_semvers) != 0:
            possible_semvers.sort(reverse=True)
            semver = possible_semvers[0]
            return f"v{semver.major}.{semver.minor}.{semver.patch}"
    except Exception as e:
        logger.error("Failed to parse version strings into semvers", network=network, error=str(e))
        return max(network_version_strings, key=len)

    return max(network_version_strings, key=len)

def fetch_cosmwasm_upgrade_proposal(rest_url, contract_address, query_type, network_name, network_repo_url):
    network_logger = logger.bind(network=network_name.upper())
    network_logger.debug(f"Attempting CosmWasm query '{query_type}' for {network_name} gov contract {contract_address} at {rest_url}")

    if query_type == "list_proposals":
        query_msg = {"list_proposals": {"limit": 20}}
    else:
        network_logger.error(f"Unsupported CosmWasm query type: {query_type}")
        return None, None, None

    query_msg_json = json.dumps(query_msg)
    query_msg_base64 = base64.b64encode(query_msg_json.encode('utf-8')).decode('utf-8')
    api_url = f"{rest_url}/cosmwasm/wasm/v1/contract/{contract_address}/smart/{query_msg_base64}"

    try:
        response = requests.get(api_url, timeout=COSMWASM_TIMEOUT_SECONDS, verify=False)
        response.raise_for_status()
        data = response.json()

        proposals_data = data.get("data", {}).get("proposals", [])
        network_logger.debug(f"Found {len(proposals_data)} proposals via CosmWasm query")

        for prop_container in reversed(proposals_data):
            proposal = prop_container.get("proposal", {})
            prop_id = prop_container.get("id")
            status = proposal.get("status", "unknown").lower()

            if status not in ["open", "passed", "executed", "neutron.cron.Schedule"]:
                network_logger.debug(f"Skipping proposal {prop_id} with status '{status}'")
                continue

            network_logger.debug(f"Checking proposal ID {prop_id} with status '{status}'")

            msgs = proposal.get("msgs", [])
            for msg_container in msgs:
                stargate_msg = msg_container.get("stargate")
                if stargate_msg:
                    type_url = stargate_msg.get("type_url")
                    value_b64 = stargate_msg.get("value")
                    if type_url == "/cosmos.upgrade.v1beta1.MsgSoftwareUpgrade" and value_b64:
                        network_logger.debug(f"Found Stargate MsgSoftwareUpgrade in proposal {prop_id}")
                        plan_name_approx, version_approx, height_approx = parse_stargate_msg_software_upgrade(value_b64)

                        if height_approx and height_approx > 0:
                            version = version_approx
                            if version:
                                network_logger.info(f"Found {network_name} upgrade via CosmWasm (Stargate): Name={plan_name_approx}, Version={version}, Height={height_approx}")
                                return plan_name_approx, version, height_approx
                            else:
                                network_logger.warning(f"Could not determine version for Stargate upgrade in prop {prop_id}")
                        continue

                wasm_execute = msg_container.get("wasm", {}).get("execute", {})
                if wasm_execute:
                    try:
                        inner_msg_b64 = wasm_execute.get("msg")
                        if inner_msg_b64:
                            inner_msg_json = base64.b64decode(inner_msg_b64).decode('utf-8')
                            inner_msg = json.loads(inner_msg_json)

                            schedule_upgrade = inner_msg.get("schedule_upgrade", {})
                            plan = schedule_upgrade.get("plan", {})
                            if plan:
                                network_logger.debug(f"Found potential Wasm 'schedule_upgrade' in proposal {prop_id}", plan=plan)
                                plan_name = plan.get("name")
                                height_str = plan.get("height")
                                info_str = plan.get("info", "")

                                height = 0
                                try:
                                    height = int(height_str)
                                except (ValueError, TypeError):
                                    network_logger.warning(f"Could not parse height '{height_str}' for Wasm upgrade in prop {prop_id}")
                                    continue

                                version = None
                                search_text = f"{plan_name} {info_str}"
                                versions = SEMANTIC_VERSION_PATTERN.findall(search_text)
                                if versions:
                                    network_repo_semver_tags = get_network_repo_semver_tags(network_name, network_repo_url)
                                    version = find_best_semver_for_versions(network_name, versions, network_repo_semver_tags)

                                if version and height > 0:
                                    network_logger.info(f"Found {network_name} upgrade via CosmWasm (Wasm Execute): Name={plan_name}, Version={version}, Height={height}")
                                    return plan_name, version, height
                                else:
                                    network_logger.debug(f"Wasm plan found in {prop_id} but missing version or valid height", name=plan_name, version=version, height=height)

                    except Exception as decode_err:
                        network_logger.debug(f"Error decoding/parsing Wasm execute msg for proposal {prop_id}", error=str(decode_err))
                        continue

        network_logger.debug("No suitable software upgrade message found in recent CosmWasm proposals.")
        return None, None, None

    except requests.exceptions.RequestException as e:
        status_code = "N/A"
        response_text = "N/A"
        if e.response is not None:
            status_code = e.response.status_code
            try:
                response_text = e.response.text
            except Exception:
                response_text = "(Could not decode response body)"

        network_logger.trace(
            f"RequestException during {network_name} CosmWasm query",
            server=rest_url,
            contract=contract_address,
            api_url=api_url,
            status_code=status_code,
            response_body=response_text[:500],
            error=str(e),
        )
        return None, None, None
    except Exception as e:
        network_logger.error(
            f"Unhandled error during {network_name} CosmWasm query",
            server=rest_url, contract=contract_address, api_url=api_url, error=str(e), trace=traceback.format_exc(),
        )
        return None, None, None


def parse_stargate_msg_software_upgrade(msg_value_base64):
    """Parses a base64 encoded MsgSoftwareUpgrade proto message."""
    try:
        decoded_bytes = base64.b64decode(msg_value_base64)
        decoded_str = decoded_bytes.decode('latin-1')
        plan_match = re.search(r'plan.*name.*"(v[^"]+)".*height.*(\d+)', decoded_str, re.IGNORECASE | re.DOTALL)
        if plan_match:
            name_approx = plan_match.group(1)
            height_approx = int(plan_match.group(2))
            logger.warning("Using unreliable regex parsing for Stargate MsgSoftwareUpgrade", name=name_approx, height=height_approx)
            return name_approx, name_approx, height_approx
    except Exception as e:
        logger.error("Failed during Stargate message parsing", error=str(e))
    return None, None, None


def fetch_data_for_networks_wrapper(network, network_type, repo_path):
    network_logger = logger.bind(network=network.upper())
    try:
        return fetch_data_for_network(network, network_type, repo_path)
    except Exception as e:
        network_logger.error("Error fetching data for network", error=str(e))
        raise e

def is_explorer_healthy(url):
    """Check if an explorer URL is healthy."""
    try:
        response = requests.get(url, timeout=EXPLORER_HEALTH_TIMEOUT_SECONDS)
        return response.status_code == 200
    except:
        return False

def get_healthy_explorer(explorers):
    """Return the healthiest explorer based on preferences."""
    for preferred in PREFERRED_EXPLORERS:
        for explorer in explorers:
            if preferred in explorer["url"] and is_explorer_healthy(explorer["url"]):
                return explorer

    for explorer in explorers:
        if is_explorer_healthy(explorer["url"]):
            return explorer

    return None

def fetch_logo_urls(data):
    """Fetch logo URLs from the chain registry data."""
    logo_uris = data.get("logo_URIs", {})
    return {
        "png": logo_uris.get("png"),
        "svg": logo_uris.get("svg")
    }

def fetch_explorer_urls(data):
    """Fetch and filter explorer URLs from the chain registry data."""
    explorers = data.get("explorers", [])
    healthy_explorer = get_healthy_explorer(explorers)
    if healthy_explorer:
        healthy_explorer.pop("tx_page", None)
        healthy_explorer.pop("account_page", None)
    return healthy_explorer

def fetch_data_for_network(network, network_type, repo_path):
    """Fetch data for a given network."""
    network_logger = logger.bind(network=network.upper())
    network_logger.trace("Starting data fetch for network")

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

    if not os.path.exists(chain_json_path):
        network_logger.error("chain.json not found for network. Skipping...")
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, chain.json not found for {network}. Consider a PR to cosmos/chain-registry"
        return err_output_data

    with open(chain_json_path, "r") as file:
        data = json.load(file)
    network_logger.trace("Loaded chain.json data")

    network_repo_url = data.get("codebase", {}).get("git_repo", None)
    network_logger.trace("Network repo URL", url=network_repo_url)

    rest_endpoints = data.get("apis", {}).get("rest", [])
    rpc_endpoints = data.get("apis", {}).get("rpc", [])

    logo_urls = fetch_logo_urls(data)
    network_logger.trace("Fetched logo URLs", urls=logo_urls)

    explorer_url = fetch_explorer_urls(data)
    network_logger.trace("Fetched explorer URL", url=explorer_url)

    latest_block_height = -1
    healthy_rpc_endpoints = get_healthy_rpc_endpoints(rpc_endpoints)
    healthy_rest_endpoints = get_healthy_rest_endpoints(rest_endpoints)
    network_logger.debug(f"Found {len(healthy_rpc_endpoints)} healthy RPC endpoints and {len(healthy_rest_endpoints)} healthy REST endpoints")
    healthy_rpc_addresses = [ep.get("address") for ep in healthy_rpc_endpoints if isinstance(ep, dict) and "address" in ep]
    network_logger.debug(f"Healthy RPC endpoints selected: {healthy_rpc_addresses}")


    if len(healthy_rpc_endpoints) == 0:
        network_logger.error(
            "No healthy RPC endpoints found. Skipping...",
            rpc_endpoints_total=len(rpc_endpoints),
        )
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, no healthy RPC servers for {network}. Consider a PR to cosmos/chain-registry"
        return err_output_data

    shuffle(healthy_rpc_endpoints)
    shuffle(healthy_rest_endpoints)

    rpc_server_used = ""
    for rpc_endpoint in healthy_rpc_endpoints:
        if not isinstance(rpc_endpoint, dict) or "address" not in rpc_endpoint:
            network_logger.debug("Invalid rpc endpoint format", rpc_endpoint=rpc_endpoint)
            continue

        network_logger.trace(f"Trying RPC endpoint for latest height: {rpc_endpoint.get('address')}")
        latest_block_height = get_latest_block_height_rpc(rpc_endpoint["address"])
        if latest_block_height > 0:
            rpc_server_used = rpc_endpoint["address"]
            network_logger.debug(f"Successfully fetched latest block height {latest_block_height} from {rpc_server_used}")
            break
        else:
            network_logger.trace(f"Failed to fetch latest block height from {rpc_endpoint.get('address')}")


    if latest_block_height < 0:
        network_logger.error(
            "No RPC endpoints returned latest height. Skipping...",
            rpc_endpoints_healthy=len(healthy_rpc_endpoints),
        )
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, no RPC servers returned latest block height for {network}. Consider a PR to cosmos/chain-registry"
        return err_output_data

    if len(healthy_rest_endpoints) == 0:
        network_logger.error(
            "No healthy REST endpoints found. Skipping...",
            rest_endpoints_total=len(rest_endpoints),
        )
        err_output_data[
            "error"
        ] = f"insufficient data in Cosmos chain registry, no healthy REST servers for {network}. Consider a PR to cosmos/chain-registry"
        err_output_data["latest_block_height"] = latest_block_height
        err_output_data["rpc_server"] = rpc_server_used
        return err_output_data

    network_logger.trace(
        "Proceeding with checks",
        rest_endpoints_count=len(healthy_rest_endpoints),
        rpc_endpoints_count=len(healthy_rpc_endpoints),
    )

    upgrade_block_height = None
    upgrade_name = ""
    upgrade_version = ""
    source = ""
    rest_server_used = ""
    output_data = {}

    for rest_endpoint in healthy_rest_endpoints:
        current_endpoint = rest_endpoint["address"]
        network_logger.trace(f"Attempting checks using REST endpoint: {current_endpoint}")

        if current_endpoint in SERVER_BLACKLIST:
            network_logger.debug(f"Skipping blacklisted REST endpoint: {current_endpoint}")
            continue

        active_upgrade_name, active_upgrade_version, active_upgrade_height = None, None, None
        current_upgrade_name, current_upgrade_version, current_upgrade_height, current_plan_dump = None, None, None, None
        cosmwasm_upgrade_name, cosmwasm_upgrade_version, cosmwasm_upgrade_height = None, None, None
        found_upgrade_on_endpoint = False

        if network not in NETWORKS_NO_GOV_MODULE:
            try:
                network_logger.debug(f"Checking standard active proposals on {current_endpoint}")
                (
                    active_upgrade_name, active_upgrade_version, active_upgrade_height
                ) = fetch_active_upgrade_proposals(current_endpoint, network, network_repo_url)
                network_logger.trace(
                    "Standard active proposal result",
                    name=active_upgrade_name,
                    version=active_upgrade_version,
                    height=active_upgrade_height
                )
            except Exception as e:
                network_logger.trace(f"Standard active proposal check failed on {current_endpoint}", error=str(e))

        if not active_upgrade_version:
            try:
                network_logger.debug(f"Checking standard current plan on {current_endpoint}")
                (
                    current_upgrade_name, current_upgrade_version, current_upgrade_height, current_plan_dump
                ) = fetch_current_upgrade_plan(current_endpoint, network, network_repo_url)
                network_logger.trace(
                    "Standard current plan result",
                    name=current_upgrade_name,
                    version=current_upgrade_version,
                    height=current_upgrade_height
                )
            except Exception as e:
                network_logger.trace(f"Standard current plan check failed on {current_endpoint}", error=str(e))

        if not active_upgrade_version and not current_upgrade_version and network in COSMWASM_GOV_CONFIG:
            config = COSMWASM_GOV_CONFIG[network]
            try:
                network_logger.debug(f"Checking CosmWasm proposals on {current_endpoint}")
                (
                    cosmwasm_upgrade_name, cosmwasm_upgrade_version, cosmwasm_upgrade_height
                ) = fetch_cosmwasm_upgrade_proposal(
                    current_endpoint,
                    config["contract_address"],
                    config["query_type"],
                    network,
                    network_repo_url
                )
                network_logger.trace(
                    "CosmWasm proposal result",
                    name=cosmwasm_upgrade_name,
                    version=cosmwasm_upgrade_version,
                    height=cosmwasm_upgrade_height
                )
            except Exception as e:
                network_logger.trace(f"CosmWasm check failed unexpectedly on {current_endpoint}", error=str(e))

        network_logger.trace("Evaluating upgrade results for endpoint...")

        if active_upgrade_version and active_upgrade_height and active_upgrade_height > latest_block_height:
            network_logger.trace(f"Using valid standard active upgrade proposal from {current_endpoint}: Height {active_upgrade_height} > {latest_block_height}")
            upgrade_block_height = active_upgrade_height
            upgrade_version = active_upgrade_version
            upgrade_name = active_upgrade_name
            source = "active_upgrade_proposals"
            found_upgrade_on_endpoint = True
        elif current_upgrade_version and current_upgrade_height and current_plan_dump and current_upgrade_height > latest_block_height:
            network_logger.trace(f"Using valid standard current upgrade plan from {current_endpoint}: Height {current_upgrade_height} > {latest_block_height}")
            upgrade_block_height = current_upgrade_height
            upgrade_version = current_upgrade_version
            upgrade_name = current_upgrade_name
            output_data["upgrade_plan"] = current_plan_dump
            source = "current_upgrade_plan"
            found_upgrade_on_endpoint = True
        elif cosmwasm_upgrade_version and cosmwasm_upgrade_height and cosmwasm_upgrade_height > latest_block_height:
            network_logger.trace(f"Using valid CosmWasm upgrade proposal from {current_endpoint}: Height {cosmwasm_upgrade_height} > {latest_block_height}")
            upgrade_block_height = cosmwasm_upgrade_height
            upgrade_version = cosmwasm_upgrade_version
            upgrade_name = cosmwasm_upgrade_name
            source = "cosmwasm_governance"
            found_upgrade_on_endpoint = True

        if found_upgrade_on_endpoint:
            rest_server_used = current_endpoint
            network_logger.info(f"Upgrade found for {network} via {source} on endpoint {rest_server_used}",
                                name=upgrade_name, version=upgrade_version, height=upgrade_block_height)
            break
        else:
            network_logger.trace(f"No valid upgrade found using endpoint {current_endpoint}. Trying next if available.")
            rest_server_used = current_endpoint

    if not upgrade_version:
        network_logger.info(f"No upgrade found for {network} after checking all {len(healthy_rest_endpoints)} healthy REST endpoint(s).")

    current_block_time = None
    past_block_time = None
    network_logger.debug("Fetching block times...")
    for rpc_endpoint in healthy_rpc_endpoints:
        if not isinstance(rpc_endpoint, dict) or "address" not in rpc_endpoint:
            network_logger.debug("Invalid rpc endpoint format for network", rpc_endpoint=rpc_endpoint)
            continue

        network_logger.trace(f"Trying RPC endpoint for block times: {rpc_endpoint.get('address')}")
        current_block_time = get_block_time_rpc(rpc_endpoint["address"], latest_block_height, network=network)
        past_block_time = get_block_time_rpc(rpc_endpoint["address"], latest_block_height - BLOCK_RANGE_FOR_AVG_TIME, network=network)

        if current_block_time and past_block_time:
            network_logger.trace(f"Successfully fetched block times from {rpc_endpoint.get('address')}")
            break
        else:
            network_logger.trace(f"Failed to fetch block times from {rpc_endpoint.get('address')}")
            continue

    if not current_block_time or not past_block_time:
        network_logger.error("Failed to fetch block times from any healthy RPC endpoint. Skipping network.")
        err_output_data["error"] = "Failed to fetch block times for estimation"
        return err_output_data

    estimated_upgrade_time = None
    if upgrade_block_height is not None:
        network_logger.debug("Estimating upgrade time...")
        estimated_upgrade_time = estimate_upgrade_time(current_block_time, past_block_time, latest_block_height, upgrade_block_height)
        network_logger.trace(f"Estimated upgrade time: {estimated_upgrade_time}")
    else:
        network_logger.trace(f"Upgrade block height is None. Skipping upgrade time estimation.")

    final_output_data = {
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
        "logo_urls": logo_urls,
        "explorer_url": explorer_url,
    }
    network_logger.debug("Completed fetch data for network", final_data=final_output_data)
    return final_output_data


# periodic cache update
def update_data():
    global_logger = logger.bind(network="GLOBAL")
    global CHAIN_WATCH
    network_blacklist_set = {net.strip().upper() for net in NETWORK_BLACKLIST if net.strip()}

    while True:
        start_time = datetime.now()
        global_logger.info("Starting data update cycle...")
        global_logger.debug(f"CHAIN_WATCH content in update_data: {CHAIN_WATCH}")

        try:
            repo_path = fetch_repo()
            global_logger.info("Repo path fetched", path=repo_path)
        except Exception as e:
            global_logger.error("Error downloading and extracting repo", error=str(e))
            global_logger.info("Sleeping for 5 seconds before retrying...")
            sleep(5)
            continue

        try:
            mainnet_networks_all = [
                d
                for d in os.listdir(repo_path)
                if os.path.isdir(os.path.join(repo_path, d))
                and not d.startswith((".", "_"))
                and d != "testnets"
            ]
            testnet_path = os.path.join(repo_path, "testnets")
            testnet_networks_all = [
                d
                for d in os.listdir(testnet_path)
                if os.path.isdir(os.path.join(testnet_path, d))
                and not d.startswith((".", "_"))
            ]
            global_logger.debug(f"Discovered mainnet networks (pre-filter): {mainnet_networks_all}")
            global_logger.debug(f"Discovered testnet networks (pre-filter): {testnet_networks_all}")

            # Filter out blacklisted networks first *before* CHAIN_WATCH filter
            mainnet_networks_filtered = [net for net in mainnet_networks_all if net.upper() not in network_blacklist_set]
            testnet_networks_filtered = [net for net in testnet_networks_all if net.upper() not in network_blacklist_set]

            # Identify which networks were actually blacklisted from the original list
            blacklisted_mainnets = [net for net in mainnet_networks_all if net.upper() in network_blacklist_set]
            blacklisted_testnets = [net for net in testnet_networks_all if net.upper() in network_blacklist_set]
            all_blacklisted = blacklisted_mainnets + blacklisted_testnets

            # Only log the blacklist info if CHAIN_WATCH is NOT set (meaning blacklist is the primary filter)
            if not CHAIN_WATCH and all_blacklisted:
                global_logger.info(f"Skipping blacklisted networks: {', '.join(sorted(all_blacklisted))}")

            # Apply CHAIN_WATCH filter if applicable (to the already blacklist-filtered lists)
            mainnet_networks = mainnet_networks_filtered
            if len(CHAIN_WATCH) != 0:
                chain_watch_lower = {c.lower() for c in CHAIN_WATCH}
                mainnet_networks = [d for d in mainnet_networks_filtered if d.lower() in chain_watch_lower]
                global_logger.debug(f"Filtered mainnet networks (post-blacklist, post-watch): {mainnet_networks}")
            else:
                # Log here if CHAIN_WATCH is not set
                global_logger.debug(f"Filtered mainnet networks (post-blacklist): {mainnet_networks}")

            testnet_networks = testnet_networks_filtered
            if len(CHAIN_WATCH) != 0:
                chain_watch_lower = {c.lower() for c in CHAIN_WATCH}
                testnet_networks = [d for d in testnet_networks_filtered if d.lower() in chain_watch_lower]
                global_logger.debug(f"Filtered testnet networks (post-blacklist, post-watch): {testnet_networks}")
            else:
                # Log here if CHAIN_WATCH is not set
                global_logger.debug(f"Filtered testnet networks (post-blacklist): {testnet_networks}")

            if not mainnet_networks and not testnet_networks:
                 # Refined warning message
                 if len(CHAIN_WATCH) > 0:
                     global_logger.warning("No matching networks found after filtering with CHAIN_WATCH and blacklist. Check CHAIN_WATCH/NETWORK_BLACKLIST values against directory names.")
                 elif all_blacklisted: # If CHAIN_WATCH is empty, but blacklist removed everything
                      global_logger.warning("All discovered networks were blacklisted.")
                 else: # Should not happen unless repo is empty/corrupt
                      global_logger.warning("No networks discovered in the repository.")


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

            cache.set("MAINNET_DATA", mainnet_data)
            cache.set("TESTNET_DATA", testnet_data)

            elapsed_time = (
                datetime.now() - start_time
            ).total_seconds()
            global_logger.info(
                "Data update cycle completed. Sleeping for 1 minute...",
                elapsed_time=elapsed_time,
            )
            sleep(UPDATE_INTERVAL_SECONDS)
        except Exception as e:
            elapsed_time = (
                datetime.now() - start_time
            ).total_seconds()
            global_logger.exception("Error in update_data loop", elapsed_time=elapsed_time, error=str(e))
            global_logger.info("Sleeping for 1 minute before retrying...")
            sleep(UPDATE_INTERVAL_SECONDS)


def start_update_data_thread():
    update_thread = threading.Thread(target=update_data)
    update_thread.daemon = True
    update_thread.start()


@app.route("/healthz")
def health_check():
    return jsonify(status="OK"), 200

@app.route("/mainnets")
def get_mainnet_data():
    results = cache.get("MAINNET_DATA")
    if results is None:
        return jsonify({"error": "Data not available"}), 500

    results = [r for r in results if r is not None]
    sorted_results = sorted(results, key=lambda x: x["upgrade_found"], reverse=True)
    reordered_results = [
        {**reorder_data(result), "logo_urls": result.get("logo_urls"), "explorer_url": result.get("explorer_url")}
        for result in sorted_results if result
    ]
    return Response(
        json.dumps(reordered_results) + "\n", content_type="application/json"
    )


@app.route("/testnets")
def get_testnet_data():
    results = cache.get("TESTNET_DATA")
    if results is None:
        return jsonify({"error": "Data not available"}), 500

    results = [r for r in results if r is not None]
    sorted_results = sorted(results, key=lambda x: x["upgrade_found"], reverse=True)
    reordered_results = [
        {**reorder_data(result), "logo_urls": result.get("logo_urls"), "explorer_url": result.get("explorer_url")}
        for result in sorted_results if result
    ]
    return Response(
        json.dumps(reordered_results) + "\n", content_type="application/json"
    )

@app.route("/chains")
def get_chains():
    """List all available chains from the chain registry."""
    try:
        repo_path = fetch_repo()
        mainnet_chains = [
            d for d in os.listdir(repo_path)
            if os.path.isdir(os.path.join(repo_path, d)) and not d.startswith((".", "_")) and d != "testnets"
        ]
        testnet_chains = [
            d for d in os.listdir(os.path.join(repo_path, "testnets"))
            if os.path.isdir(os.path.join(repo_path, "testnets", d))
        ]
        return jsonify({"mainnets": mainnet_chains, "testnets": testnet_chains}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    class RequiresGovV1Exception(Exception):
        pass
    app.debug = LOG_LEVEL == "DEBUG"

    CHAIN_WATCH = get_chain_watch_env_var()

    start_update_data_thread()
    app.run(host=FLASK_HOST, port=FLASK_PORT, use_reloader=False)
