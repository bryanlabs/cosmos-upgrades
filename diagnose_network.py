#!/usr/bin/env python3

import requests
import json
import time
import argparse
import sys
from datetime import datetime

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} | {message}")

def test_endpoint(url, endpoint_type, max_retries=3, timeout=5):
    """Test an RPC or REST endpoint with detailed metrics."""
    log(f"Testing {endpoint_type} endpoint: {url}")
    
    # Test endpoints based on type - remove the genesis block check and focus on what's important for upgrade detection
    if endpoint_type.lower() == "rpc":
        test_paths = [
            ("/status", "Status check - needed for block height"),
            ("/abci_info", "ABCI info check"),
            ("/health", "Health check"),
            # Block at a recent height, not genesis
            ("/block?height=recent", "Fetch recent block (dynamically determined)"),
            ("/validators", "Fetch validators")
        ]
    else:  # REST
        test_paths = [
            ("/cosmos/base/tendermint/v1beta1/node_info", "Node info check"),
            ("/cosmos/upgrade/v1beta1/current_plan", "Current upgrade plan - critical for detection"),
            ("/cosmos/gov/v1/proposals?proposal_status=2", "Active proposals - critical for detection")
        ]
    
    success_count = 0
    total_time = 0
    
    # If testing RPC, get a recent block height first to use for the block test
    if endpoint_type.lower() == "rpc":
        recent_height = get_recent_block_height(url)
        if recent_height:
            # Replace the placeholder with actual height
            test_paths = [
                (path.replace("recent", str(recent_height)) if "recent" in path else path, desc)
                for path, desc in test_paths
            ]
        else:
            # Remove the block test if we couldn't get a height
            test_paths = [(path, desc) for path, desc in test_paths if "recent" not in path]
    
    for path, description in test_paths:
        full_url = f"{url}{path}"
        log(f"  Checking {description} ({full_url})...")
        
        for attempt in range(1, max_retries + 1):
            try:
                start_time = time.time()
                response = requests.get(full_url, timeout=timeout, verify=False)
                end_time = time.time()
                
                response_time = end_time - start_time
                total_time += response_time
                
                if response.status_code == 200:
                    content_length = len(response.content)
                    log(f"    ✅ Success - {response_time:.3f}s - Size: {content_length} bytes")
                    success_count += 1
                    break
                else:
                    log(f"    ❌ Failed with HTTP {response.status_code} - {response_time:.3f}s")
                    if attempt < max_retries:
                        retry_wait = 2 ** attempt
                        log(f"    Retrying in {retry_wait}s...")
                        time.sleep(retry_wait)
            except requests.RequestException as e:
                end_time = time.time()
                log(f"    ❌ Request error: {str(e)} - {end_time - start_time:.3f}s")
                if attempt < max_retries:
                    retry_wait = 2 ** attempt
                    log(f"    Retrying in {retry_wait}s...")
                    time.sleep(retry_wait)
    
    # Avoid division by zero if test_paths is empty
    if len(test_paths) > 0:
        success_rate = (success_count / len(test_paths)) * 100
        avg_response_time = total_time / len(test_paths)
    else:
        success_rate = 0
        avg_response_time = 0
        
    log(f"Endpoint health summary:")
    log(f"  Success rate: {success_rate:.1f}%")
    log(f"  Average response time: {avg_response_time:.3f}s")
    
    return success_rate, avg_response_time

def get_recent_block_height(rpc_url, timeout=5):
    """Get a recent block height to test with instead of genesis."""
    try:
        log(f"  Fetching current block height to use for tests...")
        response = requests.get(f"{rpc_url}/status", timeout=timeout, verify=False)
        if response.status_code == 200:
            data = response.json()
            height = None
            
            # Handle both standard and "result" wrapped response formats
            if "result" in data and "sync_info" in data["result"]:
                height = int(data["result"]["sync_info"]["latest_block_height"])
            elif "sync_info" in data:
                height = int(data["sync_info"]["latest_block_height"])
                
            if height:
                # Use a block that's 100 blocks back for better availability
                test_height = max(1, height - 100)
                log(f"  ✅ Current height is {height}, will test with block {test_height}")
                return test_height
        
        log(f"  ❌ Could not determine current block height")
        return None
    except Exception as e:
        log(f"  ❌ Error getting block height: {str(e)}")
        return None

def test_upgrade_modules(rest_url, timeout=5):
    """Test the upgrade module endpoints specifically."""
    log(f"Testing upgrade module endpoints for {rest_url}")
    
    upgrade_paths = [
        ("/cosmos/upgrade/v1beta1/current_plan", "Current upgrade plan"),
        ("/cosmos/gov/v1/proposals?proposal_status=2", "Active proposals"),
        ("/cosmos/gov/v1/proposals?proposal_status=3", "Passed proposals"),
        ("/cosmos/gov/v1beta1/proposals?proposal_status=2", "Active proposals (v1beta1)"),
    ]
    
    for path, description in upgrade_paths:
        full_url = f"{rest_url}{path}"
        log(f"  Checking {description} ({full_url})...")
        
        try:
            start_time = time.time()
            response = requests.get(full_url, timeout=timeout, verify=False)
            duration = time.time() - start_time
            
            if response.status_code == 200:
                log(f"    ✅ Success - {duration:.3f}s")
                log(f"    Response preview: {response.text[:200]}...")
            elif response.status_code == 501:
                log(f"    ⚠️ Not implemented (501) - {duration:.3f}s")
            else:
                log(f"    ❌ Failed with HTTP {response.status_code} - {duration:.3f}s")
                try:
                    error_body = response.json()
                    log(f"    Error response: {json.dumps(error_body)[:200]}...")
                except:
                    log(f"    Error body: {response.text[:200]}...")
        except requests.RequestException as e:
            duration = time.time() - start_time
            log(f"    ❌ Request error: {str(e)} - {duration:.3f}s")

def test_block_time_computation(rpc_url, timeout=5):
    """Test the block time computation, which is essential for upgrade time estimation."""
    log(f"Testing block time computation for {rpc_url}")
    
    try:
        # Get current height
        log("  Fetching current block height...")
        start_time = time.time()
        current_response = requests.get(f"{rpc_url}/status", timeout=timeout, verify=False)
        current_duration = time.time() - start_time
        
        if current_response.status_code != 200:
            log(f"    ❌ Failed to get status - HTTP {current_response.status_code} - {current_duration:.3f}s")
            return
            
        data = current_response.json()
        current_height = None
        
        if "result" in data and "sync_info" in data["result"]:
            current_height = int(data["result"]["sync_info"]["latest_block_height"])
        elif "sync_info" in data:
            current_height = int(data["sync_info"]["latest_block_height"])
            
        if not current_height:
            log(f"    ❌ Could not determine current block height")
            return
            
        log(f"    ✅ Current height is {current_height} - {current_duration:.3f}s")
        
        # Get current block time
        log(f"  Fetching current block at height {current_height}...")
        start_time = time.time()
        response = requests.get(f"{rpc_url}/block?height={current_height}", timeout=timeout, verify=False)
        duration = time.time() - start_time
        
        if response.status_code != 200:
            log(f"    ❌ Failed to get current block - HTTP {response.status_code} - {duration:.3f}s")
        else:
            log(f"    ✅ Successfully fetched current block - {duration:.3f}s")
            
        # Get a past block for comparison (10,000 blocks ago is standard for estimation)
        past_height = max(1, current_height - 10000)
        log(f"  Fetching past block at height {past_height}...")
        start_time = time.time()
        response = requests.get(f"{rpc_url}/block?height={past_height}", timeout=timeout, verify=False)
        duration = time.time() - start_time
        
        if response.status_code != 200:
            log(f"    ❌ Failed to get past block - HTTP {response.status_code} - {duration:.3f}s")
            if response.status_code == 500:
                # Try a more recent block as the chain might have state pruning
                past_height = max(1, current_height - 1000)
                log(f"    Trying a more recent block at height {past_height}...")
                start_time = time.time()
                response = requests.get(f"{rpc_url}/block?height={past_height}", timeout=timeout, verify=False)
                duration = time.time() - start_time
                
                if response.status_code != 200:
                    log(f"    ❌ Still failed with more recent block - HTTP {response.status_code} - {duration:.3f}s")
                    log(f"  ⚠️ Warning: This node appears to have aggressive state pruning which may affect block time estimation")
                else:
                    log(f"    ✅ Successfully fetched more recent past block - {duration:.3f}s")
            else:
                log(f"  ⚠️ Warning: This node appears to have issues fetching past blocks which may affect block time estimation")
        else:
            log(f"    ✅ Successfully fetched past block - {duration:.3f}s")
            
    except requests.RequestException as e:
        duration = time.time() - start_time
        log(f"    ❌ Request error: {str(e)} - {duration:.3f}s")

def main():
    parser = argparse.ArgumentParser(description="Diagnose network endpoint performance")
    parser.add_argument("network", help="Network name (e.g., kujira, odin)")
    parser.add_argument("--rpc", help="RPC endpoint URL")
    parser.add_argument("--rest", help="REST endpoint URL")
    parser.add_argument("--timeout", type=int, default=5, help="Request timeout in seconds")
    parser.add_argument("--full", action="store_true", help="Run full diagnostics including block time tests")
    parser.add_argument("--private", action="store_true", help="Indicate these are private/high-performance endpoints")
    
    args = parser.parse_args()
    
    # Disable SSL warnings
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    log(f"Starting diagnostics for {args.network}")
    
    if args.rpc:
        log("=" * 60)
        rpc_success, rpc_avg_time = test_endpoint(args.rpc, "rpc", timeout=args.timeout)
        
        if args.full:
            log("=" * 60)
            test_block_time_computation(args.rpc, timeout=args.timeout)
    
    if args.rest:
        log("=" * 60)
        rest_success, rest_avg_time = test_endpoint(args.rest, "rest", timeout=args.timeout)
        log("=" * 60)
        test_upgrade_modules(args.rest, timeout=args.timeout)
    
    log("=" * 60)
    log("Diagnostics complete")
    
    if args.rpc and args.rest:
        if args.private:
            # For private high-performance endpoints, use tight timeouts based on actual performance
            status_timeout = max(2, int(rpc_avg_time * 1.5))
            block_fetch_timeout = max(3, int(rpc_avg_time * 2)) 
            health_check_timeout = max(2, int(min(rpc_avg_time, rest_avg_time) * 1.5))
            
            log("\nRecommended settings for your private high-performance endpoints:")
        else:
            # For public endpoints, use more conservative timeouts
            status_timeout = max(5, int(rpc_avg_time * 3))
            block_fetch_timeout = max(8, int(rpc_avg_time * 4)) 
            health_check_timeout = max(4, int(min(rpc_avg_time, rest_avg_time) * 3))
            
            log("\nRecommended settings for public endpoints (more conservative):")
            
        log(f"NETWORK_SPECIFIC_TIMEOUTS={args.network}:{status_timeout}:{block_fetch_timeout}:{health_check_timeout}")
        log(f"NETWORK_DIAGNOSTICS={args.network}")
        
        # Also provide alternative recommendation for the opposite case
        if args.private:
            alt_status = max(5, int(rpc_avg_time * 3))
            alt_block = max(8, int(rpc_avg_time * 4)) 
            alt_health = max(4, int(min(rpc_avg_time, rest_avg_time) * 3))
            log("\nAlternative settings for public endpoints (if you need to use these):")
            log(f"NETWORK_SPECIFIC_TIMEOUTS={args.network}:{alt_status}:{alt_block}:{alt_health}")
        else:
            alt_status = max(2, int(rpc_avg_time * 1.5))
            alt_block = max(3, int(rpc_avg_time * 2)) 
            alt_health = max(2, int(min(rpc_avg_time, rest_avg_time) * 1.5))
            log("\nAlternative settings for private high-performance endpoints:")
            log(f"NETWORK_SPECIFIC_TIMEOUTS={args.network}:{alt_status}:{alt_block}:{alt_health}")
        
        # Add suggestions for endpoint health checks based on results
        if rpc_avg_time > 0.1 or rest_avg_time > 0.1:
            log("\nConsider the following changes to your application:")
            log("1. Increase the number of endpoints checked (MAX_HEALTHY_ENDPOINTS)")
            log("2. Decrease the frequency of health checks or add caching")
            if args.network.lower() == "kujira":
                log("3. For Kujira specifically, check if you need to optimize CosmWasm queries")

if __name__ == "__main__":
    main()
