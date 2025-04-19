import requests
import re
from datetime import datetime
from datetime import timedelta
from random import shuffle
import traceback
import logging  # Add logging for better traceability
import threading
from flask import Flask, jsonify, request, Response
from flask_caching import Cache
from concurrent.futures import ThreadPoolExecutor
from time import sleep
import time  # for caching timestamps
from collections import OrderedDict, defaultdict  # For dynamic blacklist tracking
import os
import json
import subprocess
import semantic_version
import aiohttp
import asyncio
import socket  # Add this import for handling socket errors
from asyncio import TimeoutError  # Import TimeoutError for retries

# Initialize global variables for repo fetch frequency
repo_path = None
last_fetch_time = None  # timestamp of last repo fetch

# Initialize single HTTP session for connection pooling
session = requests.Session()
session.verify = False
# Monkey-patch requests to use pooled session
requests.get = session.get
requests.post = session.post

# Git fetch interval to reduce frequency of repo updates (default 5 minutes)
GIT_FETCH_INTERVAL = int(os.environ.get("GIT_FETCH_INTERVAL", 300))

# Cache TTL for endpoint health checks (default 10 minutes)
CACHE_TTL = int(os.environ.get("ENDPOINT_CACHE_TTL", 600))

# Caches for healthy RPC and REST endpoints: key = tuple of addresses, value = (timestamp, results)
healthy_rpc_cache = {}
healthy_rest_cache = {}

# Cache for endpoint health checks
endpoint_health_cache = {}

app = Flask(__name__)

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

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

# Dynamic blacklist for problematic endpoints
dynamic_blacklist = defaultdict(int)
BLACKLIST_THRESHOLD = 3  # Number of failures before blacklisting


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
    global last_fetch_time
    repo_clone_url = "https://github.com/cosmos/chain-registry.git"
    repo_dir = os.path.join(os.getcwd(), "chain-registry")

    # Skip fetching if the last fetch was recent
    if last_fetch_time and (datetime.now() - last_fetch_time).total_seconds() < GIT_FETCH_INTERVAL:
        print("Skipping repo fetch; last fetch was recent.")
        return repo_dir

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

    last_fetch_time = datetime.now()
    return repo_dir


# Reuse a single aiohttp.ClientSession for all asynchronous requests
class AsyncSessionManager:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()


async_session_manager = AsyncSessionManager()


async def is_rpc_endpoint_healthy_async(endpoint):
    """Asynchronous check for RPC endpoint health."""
    if endpoint in SERVER_BLACKLIST or dynamic_blacklist[endpoint] >= BLACKLIST_THRESHOLD:
        logging.warning(f"Skipping blacklisted RPC endpoint: {endpoint}")
        return endpoint, False

    if endpoint in endpoint_health_cache:
        return endpoint, endpoint_health_cache[endpoint]

    session = await async_session_manager.get_session()
    try:
        async with session.get(f"{endpoint}/abci_info", timeout=1, ssl=False) as response:
            if response.status != 200:
                async with session.get(f"{endpoint}/health", timeout=1, ssl=False) as response:
                    is_healthy = response.status == 200
            else:
                is_healthy = True
    except (socket.gaierror, TimeoutError) as e:
        logging.warning(f"Transient error for RPC endpoint {endpoint}: {e}")
        is_healthy = False
    except Exception as e:
        logging.error(f"Error checking RPC endpoint {endpoint}: {e}")
        is_healthy = False

    if not is_healthy:
        dynamic_blacklist[endpoint] += 1

    endpoint_health_cache[endpoint] = is_healthy
    return endpoint, is_healthy


async def is_rest_endpoint_healthy_async(endpoint):
    """Asynchronous check for REST endpoint health."""
    if endpoint in SERVER_BLACKLIST or dynamic_blacklist[endpoint] >= BLACKLIST_THRESHOLD:
        logging.warning(f"Skipping blacklisted REST endpoint: {endpoint}")
        return endpoint, False

    if endpoint in endpoint_health_cache:
        return endpoint, endpoint_health_cache[endpoint]

    session = await async_session_manager.get_session()
    try:
        async with session.get(f"{endpoint}/health", timeout=1, ssl=False) as response:
            if response.status != 200:
                async with session.get(
                    f"{endpoint}/cosmos/base/tendermint/v1beta1/node_info", timeout=1, ssl=False
                ) as response:
                    is_healthy = response.status == 200
            else:
                is_healthy = True
    except (socket.gaierror, TimeoutError) as e:
        logging.warning(f"Transient error for REST endpoint {endpoint}: {e}")
        is_healthy = False
    except Exception as e:
        logging.error(f"Error checking REST endpoint {endpoint}: {e}")
        is_healthy = False

    if not is_healthy:
        dynamic_blacklist[endpoint] += 1

    endpoint_health_cache[endpoint] = is_healthy
    return endpoint, is_healthy


async def get_healthy_rpc_endpoints_async(rpc_endpoints):
    """Get healthy RPC endpoints asynchronously."""
    session = await async_session_manager.get_session()
    tasks = [is_rpc_endpoint_healthy_async(rpc["address"]) for rpc in rpc_endpoints]
    results = await asyncio.gather(*tasks)
    return [rpc for rpc, is_healthy in results if is_healthy][:3]  # Limit to top 3


async def get_healthy_rest_endpoints_async(rest_endpoints):
    """Get healthy REST endpoints asynchronously."""
    session = await async_session_manager.get_session()
    tasks = [is_rest_endpoint_healthy_async(rest["address"]) for rest in rest_endpoints]
    results = await asyncio.gather(*tasks)
    return [rest for rest, is_healthy in results if is_healthy][:3]  # Limit to top 3


def fetch_data_for_network_async_wrapper(network, network_type, repo_path):
    """Wrapper to run fetch_data_for_network asynchronously with retries."""
    retries = 3
    for attempt in range(retries):
        try:
            return asyncio.run(asyncio.wait_for(fetch_data_for_network_async(network, network_type, repo_path), timeout=60))
        except asyncio.TimeoutError:
            logging.warning(f"Timeout while processing network {network}, attempt {attempt + 1}/{retries}")
        except Exception as e:
            logging.error(f"Error processing network {network}, attempt {attempt + 1}/{retries}: {e}")
        sleep(2 ** attempt)  # Exponential backoff
    logging.error(f"Failed to process network {network} after {retries} attempts")
    return None


async def fetch_data_for_network_async(network, network_type, repo_path):
    """Fetch data for a given network asynchronously."""
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

    # Use asynchronous methods for endpoint health checks
    healthy_rpc_endpoints = await get_healthy_rpc_endpoints_async(rpc_endpoints)
    healthy_rest_endpoints = await get_healthy_rest_endpoints_async(rest_endpoints)

    # Prioritize RPC endpoints for fetching the latest block height
    latest_block_height = -1
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

    for rest_endpoint in healthy_rest_endpoints:  # Fixing unpacking issue
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
                if healthy_rest_endpoints.index(rest_endpoint) + 1 < len(healthy_rest_endpoints):
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

    # Include logo_URIs and explorers in the output data
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
        "explorers": explorers,
    }
    print(f"Completed fetch data for network {network}")
    return output_data


def reorder_data(data):
    """Reorder the keys in the data dictionary for consistent output."""
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


# periodic cache update
def update_data():
    """Function to periodically update the data for mainnets and testnets."""
    global last_fetch_time
    while True:
        start_time = datetime.now()
        logging.info("Starting data update cycle...")

        # Git clone or fetch & pull
        try:
            repo_path = fetch_repo()
            logging.info(f"Repo path: {repo_path}")
        except Exception as e:
            logging.error(f"Error downloading and extracting repo: {e}")
            sleep(5)
            continue

        try:
            # Process mainnets & testnets concurrently
            mainnet_networks = [
                d for d in os.listdir(repo_path)
                if os.path.isdir(os.path.join(repo_path, d)) and not d.startswith((".", "_")) and d != "testnets"
            ]

            if len(CHAIN_WATCH) != 0:
                mainnet_networks = [d for d in mainnet_networks if d in CHAIN_WATCH]

            testnet_path = os.path.join(repo_path, "testnets")
            testnet_networks = [
                d for d in os.listdir(testnet_path)
                if os.path.isdir(os.path.join(testnet_path, d)) and not d.startswith((".", "_"))
            ]

            if len(CHAIN_WATCH) != 0:
                testnet_networks = [d for d in testnet_networks if d in CHAIN_WATCH]

            # Dynamically adjust thread pool size
            total_networks = len(mainnet_networks) + len(testnet_networks)
            pool_size = min(num_workers, total_networks)

            with ThreadPoolExecutor(max_workers=pool_size) as executor:
                testnet_data = list(
                    filter(
                        None,
                        executor.map(
                            lambda network: fetch_data_for_network_async_wrapper(
                                network, "testnet", repo_path
                            ),
                            testnet_networks,
                        ),
                    )
                )
                mainnet_data = list(
                    filter(
                        None,
                        executor.map(
                            lambda network: fetch_data_for_network_async_wrapper(
                                network, "mainnet", repo_path
                            ),
                            mainnet_networks,
                        ),
                    )
                )

            # Update the Flask cache
            cache.set("MAINNET_DATA", mainnet_data)
            cache.set("TESTNET_DATA", testnet_data)

            elapsed_time = (
                datetime.now() - start_time
            ).total_seconds()  # Calculate the elapsed time
            logging.info(
                f"Data update cycle completed in {elapsed_time} seconds. Sleeping for 1 minute..."
            )
            sleep(60)
        except Exception as e:
            elapsed_time = (
                datetime.now() - start_time
            ).total_seconds()  # Calculate the elapsed time in case of an error
            logging.error(f"Error in update_data loop after {elapsed_time} seconds: {e}")
            logging.info("Error encountered. Sleeping for 1 minute before retrying...")
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


@app.before_first_request
def initialize_async_session():
    """Ensure the aiohttp session is closed when the app shuts down."""
    import atexit

    async def close_session():
        await async_session_manager.close_session()

    atexit.register(lambda: asyncio.run(close_session()))


if __name__ == "__main__":
    app.debug = True
    start_update_data_thread()
    app.run(host="0.0.0.0", use_reloader=False)
