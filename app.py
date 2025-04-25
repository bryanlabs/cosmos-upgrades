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
LOG_FORCE_COLOR = os.environ.get("LOG_FORCE_COLOR", "false").lower() == "true"
APP_VERSION = os.environ.get("APP_VERSION", "unknown") # Load the app version
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
PRIVATE_ENDPOINTS_FILE = os.environ.get("PRIVATE_ENDPOINTS_FILE", "private_endpoints.json")
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
# --- Cache Configuration ---
CACHE_TYPE = os.environ.get("CACHE_TYPE", "simple")
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/cosmos-upgrades-cache") # Default if not set
DATA_CACHE_TIMEOUT_SECONDS = int(os.environ.get("DATA_CACHE_TIMEOUT_SECONDS", 600)) # Load the new timeout
# --- End Configuration Loading ---

# Add API key configuration
API_KEYS_FILE = os.environ.get("API_KEYS_FILE", "api_keys.json")
API_KEY_REQUIRED = os.environ.get("API_KEY_REQUIRED", "false").lower() == "true"
MAX_FREE_CHAINS = int(os.environ.get("MAX_FREE_CHAINS", "5"))

# Add network-specific timeout configuration
NETWORK_SPECIFIC_TIMEOUTS_CSV = os.environ.get("NETWORK_SPECIFIC_TIMEOUTS", "")
NETWORK_SPECIFIC_TIMEOUTS = {}
NETWORK_STATUS_TIMEOUTS = {}
NETWORK_BLOCK_FETCH_TIMEOUTS = {}
NETWORK_HEALTH_CHECK_TIMEOUTS = {}

for timeout_pair in NETWORK_SPECIFIC_TIMEOUTS_CSV.split(","):
    if timeout_pair.strip():
        parts = timeout_pair.strip().split(":")
        network = parts[0].strip()
        
        # Handle the different timeout format options
        if len(parts) >= 2:
            try:
                # Default to the same timeout for all operations if only one value is provided
                status_timeout = int(parts[1].strip())
                NETWORK_STATUS_TIMEOUTS[network] = status_timeout
                
                if len(parts) >= 3:
                    block_fetch_timeout = int(parts[2].strip())
                    NETWORK_BLOCK_FETCH_TIMEOUTS[network] = block_fetch_timeout
                else:
                    NETWORK_BLOCK_FETCH_TIMEOUTS[network] = status_timeout
                    
                if len(parts) >= 4:
                    health_check_timeout = int(parts[3].strip())
                    NETWORK_HEALTH_CHECK_TIMEOUTS[network] = health_check_timeout
                else:
                    NETWORK_HEALTH_CHECK_TIMEOUTS[network] = status_timeout
            except ValueError:
                logger.warning(f"Invalid timeout value for network {network}")

# Enable diagnostics for specific networks
NETWORK_DIAGNOSTICS_CSV = os.environ.get("NETWORK_DIAGNOSTICS", "")
NETWORK_DIAGNOSTICS = [net.strip().lower() for net in NETWORK_DIAGNOSTICS_CSV.split(",") if net.strip()]
if NETWORK_DIAGNOSTICS:
    logger.info(f"Network diagnostics enabled for: {', '.join(NETWORK_DIAGNOSTICS)}")

# Load API keys from file
api_keys = {}
try:
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE, 'r') as f:
            api_keys = json.load(f)
        logger.info(f"Loaded {len(api_keys)} API keys")
    else:
        logger.info(f"API keys file {API_KEYS_FILE} not found, API key authentication disabled")
        API_KEY_REQUIRED = False
except Exception as e:
    logger.error(f"Error loading API keys file: {str(e)}")
    API_KEY_REQUIRED = False

# Load private endpoints
private_endpoints = {}
try:
    if os.path.exists(PRIVATE_ENDPOINTS_FILE):
        with open(PRIVATE_ENDPOINTS_FILE, 'r') as f:
            private_endpoints = json.load(f)
        logger.info(f"Loaded private endpoints for {len(private_endpoints)} networks")
    else:
        logger.info(f"Private endpoints file {PRIVATE_ENDPOINTS_FILE} not found, using only chain registry endpoints")
except Exception as e:
    logger.error(f"Error loading private endpoints file: {str(e)}")

app = Flask(__name__)

# Set log level based on environment variable (already loaded)
logger.remove()
logger = logger.bind(network="GLOBAL", progress="")

# Define log formats with an additional column for progress
debug_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <yellow>{extra[progress]: <10}</yellow> | <cyan>{extra[network]}</cyan> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
info_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <yellow>{extra[progress]: <10}</yellow> | <cyan>{extra[network]}</cyan> - <level>{message}</level>"

# Add handler based on LOG_LEVEL
if LOG_LEVEL == "TRACE":
    logger.add(sys.stderr, format=debug_format, colorize=True, level=LOG_LEVEL)
elif LOG_LEVEL == "DEBUG":
    logger.add(sys.stderr, format=debug_format, colorize=True, level=LOG_LEVEL)
else:
    actual_log_level = "INFO"
    logger.add(sys.stderr, format=info_format, colorize=True, level=actual_log_level)
    LOG_LEVEL = actual_log_level

# If force color is requested, use environment variable to make loguru always colorize output
if LOG_FORCE_COLOR:
    os.environ["FORCE_COLOR"] = "1"
    logger.info("Forcing colored output for logs (useful for k9s and other tools)")

# Log the configured level and app version
logger.info(f"--- Configuration ---")
logger.info(f"App Version: {APP_VERSION}")
logger.info(f"Log Level: {LOG_LEVEL}")

# Log counts for blacklists
logger.info(f"Network Blacklist Count: {len(NETWORK_BLACKLIST)}")
logger.info(f"Server Blacklist Count: {len(SERVER_BLACKLIST)}")

# Log key operational parameters
logger.info(f"Update Interval: {UPDATE_INTERVAL_SECONDS}s")
logger.info(f"Max Healthy Endpoints: {MAX_HEALTHY_ENDPOINTS}")
logger.info(f"Worker Threads: {NUM_WORKERS}")
logger.info(f"Flask Host: {FLASK_HOST}")
logger.info(f"Flask Port: {FLASK_PORT}")

# Suppress only the single InsecureRequestWarning from urllib3
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

# Initialize cache based on config
cache_config = {
    "CACHE_TYPE": CACHE_TYPE,
}
final_cache_dir = None # Initialize variable to store the final cache dir path
if CACHE_TYPE == "filesystem":
    preferred_cache_dir = CACHE_DIR
    fallback_cache_dir = "./cache"
    final_cache_dir = preferred_cache_dir # Assume preferred initially

    # Check if the preferred directory exists and is writable
    if not os.path.exists(os.path.dirname(preferred_cache_dir)) or not os.access(os.path.dirname(preferred_cache_dir), os.W_OK):
        logger.warning(f"Preferred cache directory '{preferred_cache_dir}' is not writable or does not exist. Falling back to '{fallback_cache_dir}'.")
        final_cache_dir = fallback_cache_dir
    elif not os.path.exists(preferred_cache_dir) and not os.access(os.path.dirname(preferred_cache_dir), os.W_OK):
         logger.warning(f"Preferred cache directory parent '{os.path.dirname(preferred_cache_dir)}' is not writable. Falling back to '{fallback_cache_dir}'.")
         final_cache_dir = fallback_cache_dir
    elif os.path.exists(preferred_cache_dir) and not os.access(preferred_cache_dir, os.W_OK):
         logger.warning(f"Preferred cache directory '{preferred_cache_dir}' exists but is not writable. Falling back to '{fallback_cache_dir}'.")
         final_cache_dir = fallback_cache_dir

    cache_config["CACHE_DIR"] = final_cache_dir
    # Ensure the chosen cache directory exists
    try:
        os.makedirs(final_cache_dir, exist_ok=True)
        # Log cache settings AFTER final directory is determined
        logger.info(f"Cache Type: {CACHE_TYPE}")
        logger.info(f"Cache Directory: {final_cache_dir}") # Log the actual directory used
        logger.info(f"Data Cache Timeout: {DATA_CACHE_TIMEOUT_SECONDS}s")
        logger.info(f"Tag Cache Timeout: {TAG_CACHE_TIMEOUT_SECONDS}s")
    except OSError as e:
        logger.error(f"Failed to create cache directory '{final_cache_dir}'. Switching to in-memory cache.", error=e)
        cache_config["CACHE_TYPE"] = "simple"
        CACHE_TYPE = "simple" # Update the variable for logging below
        logger.info(f"Cache Type: {CACHE_TYPE}") # Log fallback type
        logger.info("Using simple in-memory cache")

else:
    logger.info(f"Cache Type: {CACHE_TYPE}") # Log if simple was set initially
    logger.info("Using simple in-memory cache")

logger.info(f"--- End Configuration ---") # Separator after config logs

cache = Cache(app, config=cache_config)

# Middleware for API key verification
def verify_api_key():
    if not API_KEY_REQUIRED:
        return True
        
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return False
        
    return api_key in api_keys

# Global variables (cache is now the primary source)
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

def get_network_timeout(network, default_timeout, timeout_type="status"):
    """Get network-specific timeout or fall back to default.
    
    Parameters:
    - network: The network name (string)
    - default_timeout: Default timeout to use if no specific timeout is set (int)
    - timeout_type: Type of timeout - "status", "block_fetch", or "health_check" (string)
    
    Returns:
    - Timeout value in seconds (int)
    """
    if network:
        network = network.lower()
        if timeout_type == "status" and network in NETWORK_STATUS_TIMEOUTS:
            return NETWORK_STATUS_TIMEOUTS[network]
        elif timeout_type == "block_fetch" and network in NETWORK_BLOCK_FETCH_TIMEOUTS:
            return NETWORK_BLOCK_FETCH_TIMEOUTS[network]
        elif timeout_type == "health_check" and network in NETWORK_HEALTH_CHECK_TIMEOUTS:
            return NETWORK_HEALTH_CHECK_TIMEOUTS[network]
    return default_timeout


def get_healthy_rpc_endpoints(rpc_endpoints, network=None):
    # First inject private RPC endpoints if available for this network
    private_rpcs = []
    if network and network in private_endpoints and "rpc" in private_endpoints[network]:
        for rpc_url in private_endpoints[network]["rpc"]:
            private_rpcs.append({"address": rpc_url, "private": True})
        logger.debug(f"Added {len(private_rpcs)} private RPC endpoints for {network}")
    
    # Combine private and chain registry endpoints (private first)
    combined_endpoints = private_rpcs + rpc_endpoints
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        healthy_rpc_endpoints = [
            rpc
            for rpc, is_healthy in executor.map(
                lambda rpc: (rpc, is_rpc_endpoint_healthy(rpc["address"], network)), combined_endpoints
            )
            if is_healthy
        ]

    # Log how many private endpoints are healthy
    private_healthy = sum(1 for ep in healthy_rpc_endpoints if ep.get("private", False))
    if private_rpcs:
        logger.debug(f"Found {private_healthy}/{len(private_rpcs)} healthy private RPC endpoints for {network}")
    
    return healthy_rpc_endpoints[:MAX_HEALTHY_ENDPOINTS]


def get_healthy_rest_endpoints(rest_endpoints, network=None):
    # First inject private REST endpoints if available for this network
    private_rests = []
    if network and network in private_endpoints and "rest" in private_endpoints[network]:
        for rest_url in private_endpoints[network]["rest"]:
            private_rests.append({"address": rest_url, "private": True})
        logger.debug(f"Added {len(private_rests)} private REST endpoints for {network}")
    
    # Combine private and chain registry endpoints (private first)
    combined_endpoints = private_rests + rest_endpoints
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        healthy_rest_endpoints = [
            rest
            for rest, is_healthy in executor.map(
                lambda rest: (rest, is_rest_endpoint_healthy(rest["address"], network)),
                combined_endpoints,
            )
            if is_healthy
        ]

    # Log how many private endpoints are healthy
    private_healthy = sum(1 for ep in healthy_rest_endpoints if ep.get("private", False))
    if private_rests:
        logger.debug(f"Found {private_healthy}/{len(private_rests)} healthy private REST endpoints for {network}")
    
    return healthy_rest_endpoints[:MAX_HEALTHY_ENDPOINTS]


def is_rpc_endpoint_healthy(endpoint, network=None):
    timeout = get_network_timeout(network, HEALTH_CHECK_TIMEOUT_SECONDS, "health_check")
    network_logger = logger.bind(network=network.upper() if network else "UNKNOWN", progress="")
    
    if network and network.lower() in NETWORK_DIAGNOSTICS:
        start_time = datetime.now()
        
    try:
        response = requests.get(f"{endpoint}/abci_info", timeout=timeout, verify=False)
        if response.status_code != 200:
            response = requests.get(f"{endpoint}/health", timeout=timeout, verify=False)
        result = response.status_code == 200
        
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"RPC health check for {endpoint} took {duration:.3f}s, result: {result}")
            
        return result
    except requests.exceptions.Timeout:
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"RPC health check for {endpoint} timed out after {duration:.3f}s")
        return False
    except:
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"RPC health check for {endpoint} failed after {duration:.3f}s")
        return False


def is_rest_endpoint_healthy(endpoint, network=None):
    timeout = get_network_timeout(network, HEALTH_CHECK_TIMEOUT_SECONDS, "health_check")
    network_logger = logger.bind(network=network.upper() if network else "UNKNOWN", progress="")
    
    if network and network.lower() in NETWORK_DIAGNOSTICS:
        start_time = datetime.now()
        
    try:
        response = requests.get(f"{endpoint}/health", timeout=timeout, verify=False)
        if response.status_code != 200:
            response = requests.get(
                f"{endpoint}/cosmos/base/tendermint/v1beta1/node_info",
                timeout=timeout,
                verify=False,
            )
        result = response.status_code == 200
        
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"REST health check for {endpoint} took {duration:.3f}s, result: {result}")
            
        return result
    except requests.exceptions.Timeout:
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"REST health check for {endpoint} timed out after {duration:.3f}s")
        return False
    except:
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"REST health check for {endpoint} failed after {duration:.3f}s")
        return False


def get_latest_block_height_rpc(rpc_url, network=None):
    """Fetch the latest block height from the RPC endpoint."""
    timeout = get_network_timeout(network, STATUS_TIMEOUT_SECONDS, "status")
    network_logger = logger.bind(network=network.upper() if network else "UNKNOWN", progress="")
    
    if network and network.lower() in NETWORK_DIAGNOSTICS:
        start_time = datetime.now()
        
    try:
        response = requests.get(f"{rpc_url}/status", timeout=timeout)
        response.raise_for_status()
        data = response.json()

        if "result" in data.keys():
             data = data["result"]

        height = int(data.get("sync_info", {}).get("latest_block_height", 0))
        
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"Block height fetch from {rpc_url} took {duration:.3f}s, got height {height}")
            
        return height
    except requests.exceptions.Timeout:
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"Block height fetch from {rpc_url} timed out after {duration:.3f}s")
        return -1
    except Exception as e:
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"Block height fetch from {rpc_url} failed after {duration:.3f}s: {str(e)}")
        return -1


def get_block_time_rpc(rpc_url, height, retries=None, network=None):
    """Fetch the block header time for a given block height from the RPC endpoint."""
    effective_retries = retries if retries is not None else BLOCK_TIME_RETRIES
    network_logger = logger.bind(network=network.upper() if network else "UNKNOWN", progress="")
    timeout = get_network_timeout(network, BLOCK_FETCH_TIMEOUT_SECONDS, "block_fetch")
    
    if network and network.lower() in NETWORK_DIAGNOSTICS:
        overall_start_time = datetime.now()
        
    for attempt in range(effective_retries):
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            attempt_start_time = datetime.now()
            
        try:
            response = requests.get(f"{rpc_url}/block?height={height}", timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if "result" in data.keys():
                data = data["result"]
            block_time = data.get("block", {}).get("header", {}).get("time", "")
            
            if network and network.lower() in NETWORK_DIAGNOSTICS:
                attempt_duration = (datetime.now() - attempt_start_time).total_seconds()
                network_logger.debug(f"Block time fetch from {rpc_url} (height {height}) succeeded on attempt {attempt+1} in {attempt_duration:.3f}s")
                
            return block_time
        except requests.exceptions.HTTPError as e:
            if network and network.lower() in NETWORK_DIAGNOSTICS:
                attempt_duration = (datetime.now() - attempt_start_time).total_seconds()
                network_logger.debug(f"HTTP error on attempt {attempt+1} for {rpc_url}: {str(e)} after {attempt_duration:.3f}s")
                
            if attempt == effective_retries - 1:
                return None
        except Exception as e:
            if network and network.lower() in NETWORK_DIAGNOSTICS:
                attempt_duration = (datetime.now() - attempt_start_time).total_seconds()
                network_logger.debug(f"Attempt {attempt+1} failed for {rpc_url}: {str(e)} after {attempt_duration:.3f}s")
                
            if attempt == effective_retries - 1:
                return None
                
        sleep(2 ** attempt)
        
    if network and network.lower() in NETWORK_DIAGNOSTICS:
        overall_duration = (datetime.now() - overall_start_time).total_seconds()
        network_logger.debug(f"Block time fetch from {rpc_url} (height {height}) failed after {overall_duration:.3f}s and {effective_retries} attempts")
        
    return None


def fetch_active_upgrade_proposals(rest_url, network, network_repo_url):
    network_logger = logger.bind(network=network.upper(), progress="")
    
    if network and network.lower() in NETWORK_DIAGNOSTICS:
        start_time = datetime.now()
        
    try:
        [plan_name, version, height] = fetch_active_upgrade_proposals_v1(rest_url, network, network_repo_url)
        
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"Active upgrade proposals check took {duration:.3f}s")
            
    except RequiresGovV1Exception as e:
        [plan_name, version, height] = fetch_active_upgrade_proposals_v1(rest_url, network, network_repo_url)
        
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"Active upgrade proposals check with v1 fallback took {duration:.3f}s")
            
    except Exception as e:
        if network and network.lower() in NETWORK_DIAGNOSTICS:
            duration = (datetime.now() - start_time).total_seconds()
            network_logger.debug(f"Active upgrade proposals check failed after {duration:.3f}s: {str(e)}")
            
        raise e
    
    return plan_name, version, height


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


# Modify fetch_data_for_network to accept progress_text
def fetch_data_for_network(network, network_type, repo_path, custom_logger=None, progress_text=""):
    """Fetch data for a given network."""
    # Use the provided progress_text when binding the logger
    network_logger = custom_logger or logger.bind(network=network.upper(), progress=progress_text)
    network_logger.trace("Starting data fetch for network")
    
    # Add timing instrumentation
    start_time = datetime.now()
    
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
    
    # Add timing for RPC endpoints health check
    rpc_health_start = datetime.now()
    healthy_rpc_endpoints = get_healthy_rpc_endpoints(rpc_endpoints, network)
    rpc_health_duration = (datetime.now() - rpc_health_start).total_seconds()
    network_logger.debug(f"RPC health check took {rpc_health_duration:.2f}s, found {len(healthy_rpc_endpoints)} healthy endpoints")
    
    # Add timing for REST endpoints health check
    rest_health_start = datetime.now()
    healthy_rest_endpoints = get_healthy_rest_endpoints(rest_endpoints, network)
    rest_health_duration = (datetime.now() - rest_health_start).total_seconds()
    network_logger.debug(f"REST health check took {rest_health_duration:.2f}s, found {len(healthy_rest_endpoints)} healthy endpoints")
    
    network_logger.debug(f"Found {len(healthy_rpc_endpoints)} healthy RPC endpoints and {len(healthy_rest_endpoints)} healthy REST endpoints")
    
    # Log private endpoints separately
    private_rpc_count = sum(1 for ep in healthy_rpc_endpoints if ep.get("private", False))
    private_rest_count = sum(1 for ep in healthy_rest_endpoints if ep.get("private", False))
    if private_rpc_count or private_rest_count:
        network_logger.info(f"Using {private_rpc_count} private RPC and {private_rest_count} private REST endpoints for {network}")
    
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

    # Add timing for block height fetching
    block_height_start = datetime.now()
    rpc_server_used = ""
    for rpc_endpoint in healthy_rpc_endpoints:
        if not isinstance(rpc_endpoint, dict) or "address" not in rpc_endpoint:
            network_logger.debug("Invalid rpc endpoint format", rpc_endpoint=rpc_endpoint)
            continue

        network_logger.trace(f"Trying RPC endpoint for latest height: {rpc_endpoint.get('address')}")
        height_fetch_start = datetime.now()
        latest_block_height = get_latest_block_height_rpc(rpc_endpoint["address"], network)
        height_fetch_duration = (datetime.now() - height_fetch_start).total_seconds()
        
        if latest_block_height > 0:
            rpc_server_used = rpc_endpoint["address"]
            network_logger.debug(f"Block height fetch took {height_fetch_duration:.2f}s, got height {latest_block_height} from {rpc_server_used}")
            break
        else:
            network_logger.trace(f"Failed to fetch latest block height from {rpc_endpoint.get('address')} after {height_fetch_duration:.2f}s")
            
    block_height_duration = (datetime.now() - block_height_start).total_seconds()
    network_logger.debug(f"Total block height fetching took {block_height_duration:.2f}s")
    
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
    # Initialize upgrade status message with default (no upgrade found)
    upgrade_status_message = f"No upgrade found (checked {len(healthy_rest_endpoints)} endpoint(s))" 

    # Optimize proposal checks by doing one per REST endpoint
    for rest_endpoint in healthy_rest_endpoints:
        current_endpoint = rest_endpoint["address"]
        endpoint_start = datetime.now()
        
        network_logger.trace(f"Attempting checks using REST endpoint: {current_endpoint}")

        if current_endpoint in SERVER_BLACKLIST:
            network_logger.debug(f"Skipping blacklisted REST endpoint: {current_endpoint}")
            continue

        active_upgrade_name, active_upgrade_version, active_upgrade_height = None, None, None
        current_upgrade_name, current_upgrade_version, current_upgrade_height, current_plan_dump = None, None, None, None
        cosmwasm_upgrade_name, cosmwasm_upgrade_version, cosmwasm_upgrade_height = None, None, None
        found_upgrade_on_endpoint = False
        
        # Check current upgrade plan first (fastest endpoint based on diagnostics)
        try:
            plan_check_start = datetime.now()
            network_logger.debug(f"Checking standard current plan on {current_endpoint}")
            (
                current_upgrade_name, current_upgrade_version, current_upgrade_height, current_plan_dump
            ) = fetch_current_upgrade_plan(current_endpoint, network, network_repo_url)
            plan_check_duration = (datetime.now() - plan_check_start).total_seconds()
            network_logger.debug(f"Current plan check took {plan_check_duration:.2f}s")
            
            if current_upgrade_version and current_upgrade_height and current_plan_dump and current_upgrade_height > latest_block_height:
                network_logger.debug(f"Found valid upgrade in current plan")
                upgrade_block_height = current_upgrade_height
                upgrade_version = current_upgrade_version
                upgrade_name = current_upgrade_name
                output_data["upgrade_plan"] = current_plan_dump
                source = "current_upgrade_plan"
                # Update status message
                upgrade_status_message = f"Upgrade '{upgrade_name}' found at block {upgrade_block_height} via {source}"
                found_upgrade_on_endpoint = True
                rest_server_used = current_endpoint
            else:
                network_logger.trace("No valid upgrade found in current plan, continuing with other checks")
        except Exception as e:
            network_logger.trace(f"Standard current plan check failed on {current_endpoint}", error=str(e))

        # Only check active proposals if needed and if gov module is supported
        if not found_upgrade_on_endpoint and network not in NETWORKS_NO_GOV_MODULE:
            try:
                gov_check_start = datetime.now()
                network_logger.debug(f"Checking standard active proposals on {current_endpoint}")
                (
                    active_upgrade_name, active_upgrade_version, active_upgrade_height
                ) = fetch_active_upgrade_proposals(current_endpoint, network, network_repo_url)
                gov_check_duration = (datetime.now() - gov_check_start).total_seconds()
                network_logger.debug(f"Gov proposals check took {gov_check_duration:.2f}s")
                
                if active_upgrade_version and active_upgrade_height and active_upgrade_height > latest_block_height:
                    network_logger.debug(f"Found valid upgrade in active proposals")
                    upgrade_block_height = active_upgrade_height
                    upgrade_version = active_upgrade_version
                    upgrade_name = active_upgrade_name
                    source = "active_upgrade_proposals"
                    # Update status message
                    upgrade_status_message = f"Upgrade '{upgrade_name}' found at block {upgrade_block_height} via {source}"
                    found_upgrade_on_endpoint = True
                    rest_server_used = current_endpoint
                else:
                    network_logger.trace("No valid upgrade found in active proposals, continuing with other checks")
            except Exception as e:
                network_logger.trace(f"Standard active proposal check failed on {current_endpoint}", error=str(e))

        # Only check CosmWasm if needed and if configured for this network
        if not found_upgrade_on_endpoint and network in COSMWASM_GOV_CONFIG:
            config = COSMWASM_GOV_CONFIG[network]
            try:
                cosmwasm_check_start = datetime.now()
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
                cosmwasm_check_duration = (datetime.now() - cosmwasm_check_start).total_seconds()
                network_logger.debug(f"CosmWasm proposals check took {cosmwasm_check_duration:.2f}s")
                
                if cosmwasm_upgrade_version and cosmwasm_upgrade_height and cosmwasm_upgrade_height > latest_block_height:
                    network_logger.debug(f"Found valid upgrade in CosmWasm proposals")
                    upgrade_block_height = cosmwasm_upgrade_height
                    upgrade_version = cosmwasm_upgrade_version
                    upgrade_name = cosmwasm_upgrade_name
                    source = "cosmwasm_governance"
                    # Update status message
                    upgrade_status_message = f"Upgrade '{upgrade_name}' found at block {upgrade_block_height} via {source}"
                    found_upgrade_on_endpoint = True
                    rest_server_used = current_endpoint
                else:
                    network_logger.trace("No valid upgrade found in CosmWasm proposals")
            except Exception as e:
                network_logger.trace(f"CosmWasm check failed unexpectedly on {current_endpoint}", error=str(e))

        endpoint_duration = (datetime.now() - endpoint_start).total_seconds()
        network_logger.debug(f"REST endpoint {current_endpoint} checks took {endpoint_duration:.2f}s")
        
        if found_upgrade_on_endpoint:
            break # Exit loop once upgrade is found

    # After the loop, if no upgrade was found, ensure rest_server_used is set for the final data
    if not found_upgrade_on_endpoint:
        rest_server_used = healthy_rest_endpoints[0]["address"] if healthy_rest_endpoints else ""

    # Estimate upgrade time if an upgrade was found
    block_time_fetch_start = datetime.now()
    current_block_time = None
    past_block_time = None
    network_logger.debug("Fetching block times...")
    
    for rpc_endpoint in healthy_rpc_endpoints:
        if not isinstance(rpc_endpoint, dict) or "address" not in rpc_endpoint:
            network_logger.debug("Invalid rpc endpoint format for network", rpc_endpoint=rpc_endpoint)
            continue

        network_logger.trace(f"Trying RPC endpoint for block times: {rpc_endpoint.get('address')}")
        fetch_current_start = datetime.now()
        current_block_time = get_block_time_rpc(rpc_endpoint["address"], latest_block_height, network=network)
        current_time_duration = (datetime.now() - fetch_current_start).total_seconds()
        
        if current_block_time:
            network_logger.trace(f"Successfully fetched current block time in {current_time_duration:.2f}s")
            fetch_past_start = datetime.now()
            past_block_time = get_block_time_rpc(rpc_endpoint["address"], latest_block_height - BLOCK_RANGE_FOR_AVG_TIME, network=network)
            past_time_duration = (datetime.now() - fetch_past_start).total_seconds()
            
            if past_block_time:
                network_logger.trace(f"Successfully fetched past block time in {past_time_duration:.2f}s")
                break
            else:
                network_logger.trace(f"Failed to fetch past block time, trying next endpoint")
        else:
            network_logger.trace(f"Failed to fetch current block time, trying next endpoint")
            continue
    
    block_time_fetch_duration = (datetime.now() - block_time_fetch_start).total_seconds()
    network_logger.debug(f"Block time fetching took {block_time_fetch_duration:.2f}s total")

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
    
    # Log the combined completion message (network_logger already has the progress_text bound)
    total_duration = (datetime.now() - start_time).total_seconds()
    network_logger.info(f"Completed fetch data for {network} in {total_duration:.2f}s - {upgrade_status_message}") 
    network_logger.debug("Completed fetch data for network", final_data=final_output_data)
    return final_output_data


# periodic cache update
def update_data():
    global_logger = logger.bind(network="GLOBAL", progress="")
    global CHAIN_WATCH
    network_blacklist_set = {net.strip().upper() for net in NETWORK_BLACKLIST if net.strip()}

    while True:
        start_time = datetime.now()
        global_logger.info("Starting data update cycle...")
        global_logger.debug(f"CHAIN_WATCH content in update_data: {CHAIN_WATCH}")

        try:
            repo_path = fetch_repo()
            global_logger.info("Repo path fetched")
            sleep(0.1)
        except Exception as e:
            global_logger.error("Error downloading and extracting repo", error=str(e))
            global_logger.info(f"Sleeping for {UPDATE_INTERVAL_SECONDS} seconds before retrying...")
            sleep(UPDATE_INTERVAL_SECONDS)
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

            mainnet_networks_filtered = [net for net in mainnet_networks_all if net.upper() not in network_blacklist_set]
            testnet_networks_filtered = [net for net in testnet_networks_all if net.upper() not in network_blacklist_set]

            blacklisted_mainnets = [net for net in mainnet_networks_all if net.upper() in network_blacklist_set]
            blacklisted_testnets = [net for net in testnet_networks_all if net.upper() in network_blacklist_set]
            all_blacklisted = blacklisted_mainnets + blacklisted_testnets

            if not CHAIN_WATCH and all_blacklisted:
                global_logger.info(f"Skipping blacklisted networks: {', '.join(sorted(all_blacklisted))}")

            mainnet_networks = mainnet_networks_filtered
            if len(CHAIN_WATCH) != 0:
                chain_watch_lower = {c.lower() for c in CHAIN_WATCH}
                mainnet_networks = [d for d in mainnet_networks_filtered if d.lower() in chain_watch_lower]
                global_logger.debug(f"Filtered mainnet networks (post-blacklist, post-watch): {mainnet_networks}")
            else:
                global_logger.debug(f"Filtered mainnet networks (post-blacklist): {mainnet_networks}")

            testnet_networks = testnet_networks_filtered
            if len(CHAIN_WATCH) != 0:
                chain_watch_lower = {c.lower() for c in CHAIN_WATCH}
                testnet_networks = [d for d in testnet_networks_filtered if d.lower() in chain_watch_lower]
                global_logger.debug(f"Filtered testnet networks (post-blacklist, post-watch): {testnet_networks}")
            else:
                global_logger.debug(f"Filtered testnet networks (post-blacklist): {testnet_networks}")

            if not mainnet_networks and not testnet_networks:
                 if len(CHAIN_WATCH) > 0:
                     global_logger.warning("No matching networks found after filtering with CHAIN_WATCH and blacklist. Check CHAIN_WATCH/NETWORK_BLACKLIST values against directory names.")
                 elif all_blacklisted:
                      global_logger.warning("All discovered networks were blacklisted.")
                 else:
                      global_logger.warning("No networks discovered in the repository.")

            sleep(0.1)
            
            total_networks = len(testnet_networks) + len(mainnet_networks)
            global_logger.info(f"Processing {total_networks} networks ({len(mainnet_networks)} mainnets, {len(testnet_networks)} testnets)")
            
            # Create progress counter and lock
            completed_networks = 0
            progress_lock = threading.Lock()
            
            # Modify the process_network_with_progress function
            def process_network_with_progress(network, network_type):
                # Calculate progress text first
                nonlocal completed_networks
                with progress_lock:
                    completed_networks += 1
                    percent = completed_networks / total_networks * 100 if total_networks > 0 else 100
                    progress_text = f"{completed_networks}/{total_networks}" # Store progress text

                result = None
                try:
                    # Pass progress_text to fetch_data_for_network
                    result = fetch_data_for_network(network, network_type, repo_path, progress_text=progress_text)
                except Exception as e:
                    # Log error with network context, progress will be empty here
                    error_logger = logger.bind(network=network.upper(), progress="") 
                    error_logger.error(f"Error processing network {network}: {str(e)}")
                
                return result
            
            # Add a watchdog timer to detect if processing hangs
            def watchdog_timer():
                last_completed = 0
                while completed_networks < total_networks:
                    sleep(60)  # Check every minute
                    if completed_networks == last_completed:
                        global_logger.warning(f"Processing appears to be stuck at {completed_networks}/{total_networks} networks")
                    last_completed = completed_networks

            # Start the watchdog in a separate thread
            watchdog_thread = threading.Thread(target=watchdog_timer)
            watchdog_thread.daemon = True
            watchdog_thread.start()

            with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
                # Process testnet networks
                global_logger.debug(f"Submitting {len(testnet_networks)} testnet networks to thread pool")
                testnet_data = list(
                    filter(
                        None,
                        executor.map(
                            lambda network: process_network_with_progress(network, "testnet"),
                            testnet_networks,
                        ),
                    )
                )
                
                # Process mainnet networks
                global_logger.debug(f"Submitting {len(mainnet_networks)} mainnet networks to thread pool")
                mainnet_data = list(
                    filter(
                        None,
                        executor.map(
                            lambda network: process_network_with_progress(network, "mainnet"),
                            mainnet_networks,
                        ),
                    )
                )

            # Set data in cache with timeout based on DATA_CACHE_TIMEOUT_SECONDS
            cache.set("MAINNET_DATA", mainnet_data, timeout=DATA_CACHE_TIMEOUT_SECONDS)
            cache.set("TESTNET_DATA", testnet_data, timeout=DATA_CACHE_TIMEOUT_SECONDS)

            # Log completion
            elapsed_time = (datetime.now() - start_time).total_seconds()
            minutes, seconds = divmod(int(elapsed_time), 60)
            time_format = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            avg_time_per_network = elapsed_time / total_networks if total_networks > 0 else 0
            
            global_logger.info(
                f"Data update cycle completed in {time_format} (avg: {avg_time_per_network:.2f}s per network). Sleeping for {UPDATE_INTERVAL_SECONDS} seconds...",
                elapsed_time=elapsed_time,
            )
            sleep(UPDATE_INTERVAL_SECONDS)
        except Exception as e:
            elapsed_time = (datetime.now() - start_time).total_seconds()
            global_logger.exception("Error in update_data loop", elapsed_time=elapsed_time, error=str(e))
            global_logger.info(f"Sleeping for {UPDATE_INTERVAL_SECONDS} seconds before retrying...")
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
    # API key verification for premium access
    if API_KEY_REQUIRED and not verify_api_key():
        # For free tier, limit the number of chains
        results = cache.get("MAINNET_DATA")
        if results and isinstance(results, list):
            results = results[:MAX_FREE_CHAINS]  # Limit to MAX_FREE_CHAINS for free tier
            sorted_results = sorted(results, key=lambda x: x.get("upgrade_found", False), reverse=True)
            reordered_results = [
                {**reorder_data(result), "logo_urls": result.get("logo_urls"), "explorer_url": result.get("explorer_url")}
                for result in sorted_results if result
            ]
            # Add message about limited access
            response_data = {
                "data": reordered_results,
                "message": f"Free tier limited to {MAX_FREE_CHAINS} chains. Sign up for premium access.",
                "limited": True
            }
            return Response(json.dumps(response_data) + "\n", content_type="application/json")
    # Full access for API key holders
    results = cache.get("MAINNET_DATA")
    if results is None:
        # Data not in cache (either first run with no persistent data, or expired)
        # Return empty list while background update runs
        logger.warning("Mainnet data not found in cache or expired. Background update pending.")
        return Response(json.dumps([]) + "\n", content_type="application/json")
    
    # Ensure results is a list, even if cache somehow returns non-list
    if not isinstance(results, list):
         logger.error(f"Unexpected data type found in mainnet cache: {type(results)}. Returning empty list.")
         return Response(json.dumps([]) + "\n", content_type="application/json")
    results = [r for r in results if r is not None]
    sorted_results = sorted(results, key=lambda x: x.get("upgrade_found", False), reverse=True) # Added .get for safety
    reordered_results = [
        {**reorder_data(result), "logo_urls": result.get("logo_urls"), "explorer_url": result.get("explorer_url")}
        for result in sorted_results if result
    ]
    return Response(
        json.dumps(reordered_results) + "\n", content_type="application/json"
    )

@app.route("/testnets")
def get_testnet_data():
    # API key verification for premium access
    if API_KEY_REQUIRED and not verify_api_key():
        # For free tier, limit the number of chains
        results = cache.get("TESTNET_DATA")
        if results and isinstance(results, list):
            results = results[:MAX_FREE_CHAINS]  # Limit to MAX_FREE_CHAINS for free tier
            sorted_results = sorted(results, key=lambda x: x.get("upgrade_found", False), reverse=True)
            reordered_results = [
                {**reorder_data(result), "logo_urls": result.get("logo_urls"), "explorer_url": result.get("explorer_url")}
                for result in sorted_results if result
            ]
            # Add message about limited access
            response_data = {
                "data": reordered_results,
                "message": f"Free tier limited to {MAX_FREE_CHAINS} chains. Sign up for premium access.",
                "limited": True
            }
            return Response(json.dumps(response_data) + "\n", content_type="application/json")
    # Full access for API key holders
    results = cache.get("TESTNET_DATA")
    if results is None:
        # Data not in cache (either first run with no persistent data, or expired)
        # Return empty list while background update runs
        logger.warning("Testnet data not found in cache or expired. Background update pending.")
        return Response(json.dumps([]) + "\n", content_type="application/json")
    
    # Ensure results is a list, even if cache somehow returns non-list
    if not isinstance(results, list):
         logger.error(f"Unexpected data type found in testnet cache: {type(results)}. Returning empty list.")
         return Response(json.dumps([]) + "\n", content_type="application/json")
    results = [r for r in results if r is not None]
    sorted_results = sorted(results, key=lambda x: x.get("upgrade_found", False), reverse=True) # Added .get for safety
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

@app.route("/admin/api-keys", methods=["POST"])
def manage_api_keys():
    admin_key = request.headers.get('X-Admin-Key')
    if not admin_key or admin_key != os.environ.get("ADMIN_API_KEY"):
        return jsonify({"error": "Unauthorized"}), 401

    # Handle API key operations:
    operation = request.json.get("operation")
    key = request.json.get("key")
    if operation == "add" and key:
        api_keys[key] = {"created": datetime.utcnow().isoformat(), "active": True}
    elif operation == "revoke" and key:
        if key in api_keys:
            api_keys[key]["active"] = False
    elif operation == "delete" and key:
        if key in api_keys:
            del api_keys[key]

    # Save updated keys
    with open(API_KEYS_FILE, 'w') as f:
        json.dump(api_keys, f)

    return jsonify({"status": "success"}), 200

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

if __name__ == "__main__":
    class RequiresGovV1Exception(Exception):
        pass

    app.debug = LOG_LEVEL == "DEBUG"

    CHAIN_WATCH = get_chain_watch_env_var()

    start_update_data_thread()
    app.run(host=FLASK_HOST, port=FLASK_PORT, use_reloader=False)
