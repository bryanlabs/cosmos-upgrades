# cosmos-upgrades

`cosmos-upgrades` is a powerful tool developed by [bryanlabs](https://github.com/bryanlabs) to search for scheduled Cosmos upgrades. This tool aims to streamline the process of tracking and managing upgrades in the Cosmos ecosystem.

## 🌌 Introduction

The Cosmos ecosystem is vast and ever-evolving. With frequent upgrades and enhancements, it becomes crucial for stakeholders to keep track of scheduled upgrades. `cosmos-upgrades` bridges this gap by providing a centralized solution to fetch and monitor these upgrades.

## 🛠 Problem Statement

Keeping track of scheduled upgrades in a decentralized ecosystem can be challenging. Missing an upgrade can lead to potential downtimes, security vulnerabilities, and missed opportunities. `cosmos-upgrades` addresses this challenge by offering a reliable and up-to-date source of information for all scheduled Cosmos upgrades.

## 📚 Chain-Registry Deep Dive

The `chain-registry` is more than just a repository of chain details; it's the backbone that powers the `cosmos-upgrades` tool. Each chain specified in the request is mapped to its corresponding JSON file within the `chain-registry`. This mapping allows the tool to look up vital information, such as endpoints, for each chain.

For instance, when you specify "akash" in your request, the tool refers to the [`akash/chain.json`](https://github.com/cosmos/chain-registry/blob/master/akash/chain.json) file in the `chain-registry` to fetch the necessary details.

### Why is the Chain-Registry Essential?

1. **Accuracy & Reliability:** By centralizing the details of all chains in the `chain-registry`, we ensure that the data fetched by `cosmos-upgrades` is always accurate and up-to-date.
2. **Extensibility:** The design of the `chain-registry` allows for easy additions of new chains or updates to existing ones.
3. **Community Collaboration:** The `chain-registry` is open-source, fostering a collaborative environment. If a user notices a missing chain or outdated information, they can contribute by submitting a PR with the correct details.

### What if a Network is Missing?

If a particular network or chain is not present in the `chain-registry`, the `cosmos-upgrades` tool won't be able to provide information about it. In such cases, we strongly encourage users to:

- Reach out to the protocol leads to inform them about the omission.
- Take a proactive approach by submitting a PR to the `chain-registry` with the correct information.

By doing so, not only do you enhance the tool's capabilities, but you also contribute to the broader Cosmos community.

## 🔒 Using Private Endpoints

If you operate your own RPC and REST endpoints for Cosmos chains, you can configure the tool to prioritize these private endpoints over the ones in the chain registry.

### Setting Up Private Endpoints

1. Create or modify the `private_endpoints.json` file in the root directory with your private endpoint information:

```json
{
  "osmosis": {
    "rpc": ["https://my-private-osmosis-rpc.example.com"],
    "rest": ["https://my-private-osmosis-rest.example.com"]
  },
  "cosmoshub": {
    "rpc": ["https://my-private-cosmoshub-rpc.example.com:26657", "https://my-backup-cosmoshub-rpc.example.com:26657"],
    "rest": ["https://my-private-cosmoshub-rest.example.com:1317"]
  }
}
```

2. The tool will automatically detect and prioritize these endpoints during the data fetching process.

3. You can specify a custom path for this file using the `PRIVATE_ENDPOINTS_FILE` environment variable in `.env`:

```
PRIVATE_ENDPOINTS_FILE=/path/to/my/private_endpoints.json
```

### Benefits of Private Endpoints

- **Reliability:** Use your own infrastructure for critical monitoring
- **Reduced Latency:** Private endpoints may be faster than public ones
- **Customization:** Easily switch between different endpoint configurations

The tool will always check your private endpoints first, and fall back to chain registry endpoints if yours are unavailable.

## 🚀 Making Requests

To fetch the scheduled upgrades, you can use the following `curl` command for both mainnets and testnets:

### Mainnets

```bash
curl -s -X GET \
  -H "Content-Type: application/json" \
  https://cosmos-upgrades.apis.bryanlabs.net/mainnets
```

### Testnets

```bash
curl -s -X GET \
  -H "Content-Type: application/json" \
  https://cosmos-upgrades.apis.bryanlabs.net/testnets
```

**Note:** The response will contain details of the scheduled upgrades for the specified networks.

## 🧪 Automated Script (`upgrades.sh`)

`upgrades.sh` is a convenient script provided to fetch scheduled upgrades for both mainnets and testnets. It offers customization options and simplifies the process of tracking upgrades.

### Usage

1. Make sure you have `jq` installed on your system. You can install it using your system's package manager.

2. Open a terminal and navigate to the directory containing `upgrades.sh`.

3. Run the script to fetch upgrades for both mainnets and testnets:

```bash
./upgrades.sh
```

The script will provide you with a list of scheduled upgrades for the specified networks.

### Customizing Networks

You can customize the list of networks by modifying the `networks` associative array in the script. The `networks` array is divided into `mainnets` and `testnets`, and you can add or remove network names as needed.

```bash
declare -A networks=(
  [mainnets]="secretnetwork osmosis neutron nolus crescent akash cosmoshub sentinel stargaze omniflixhub cosmoshub terra kujira stride injective juno agoric evmos noble omny quasar dvpn onomy"
  [testnets]="agorictestnet quasartestnet stridetestnet onomytestnet axelartestnet nibirutestnet nobletestnet dydxtestnet osmosistestnet cosmoshubtestnet"
)
```

### `CHAIN_WATCH` Environment Variable

The `CHAIN_WATCH` environment variable allows you to specify a particular chain(s) to use, instead of all. If set, the app will only poll the chain-regsitry for the specified chain(s). Otherwise, it will poll all chains in the registry. You can still filter this output with other tooling like upgrades.sh

For example, to only poll "cosmoshub" rpc/rest endpoints, you can set `CHAIN_WATCH` as follows:

```bash
export CHAIN_WATCH="cosmoshub"
```
