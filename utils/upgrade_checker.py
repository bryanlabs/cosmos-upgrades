import re
import json
import requests
import traceback
from time import sleep
from loguru import logger

SEMANTIC_VERSION_PATTERN = re.compile(r"(v\d+(?:\.\d+){0,2})")

class RequiresGovV1Exception(Exception):
    """Exception raised when v1beta1 endpoint returns an error that suggests v1 is required."""
    pass

def check_proposal_for_upgrade(proposal, network, network_repo_url, get_network_repo_semver_tags, find_best_semver_for_versions):
    """
    Checks a single proposal for upgrade information
    
    Returns:
        Tuple of (plan_name, version, height) or (None, None, None) if no upgrade is found
    """
    network_logger = logger.bind(network=network.upper())
    proposal_id = proposal.get("id", "N/A")
    
    # For v1 proposals
    messages = proposal.get("messages", [])
    for message in messages:
        proposal_type = message.get("@type")
        upgrade_content = None

        if (
            proposal_type == "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal" or
            proposal_type == '/cosmos.upgrade.v1beta1.MsgSoftwareUpgrade'
        ):
            network_logger.trace(f"Found direct upgrade message in proposal {proposal_id}", message_data=message)
            upgrade_content = message

        elif proposal_type == "/cosmos.gov.v1.MsgExecLegacyContent":
            network_logger.trace(f"Found MsgExecLegacyContent in proposal {proposal_id}, checking nested content...")
            legacy_content = message.get("content")
            if legacy_content and legacy_content.get("@type") == "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal":
                network_logger.trace(f"Found nested SoftwareUpgradeProposal in proposal {proposal_id}", message_data=legacy_content)
                upgrade_content = legacy_content
            else:
                network_logger.trace(f"Nested content is not a SoftwareUpgradeProposal in proposal {proposal_id}")

        if upgrade_content:
            plan = upgrade_content.get("plan", {})
            plan_name = plan.get("name", "")

            content_dump = json.dumps(upgrade_content)
            network_logger.trace(f"Content dump for version search (prop {proposal_id})", content=content_dump)

            versions = SEMANTIC_VERSION_PATTERN.findall(content_dump)
            if not versions and plan_name:
                versions.extend(SEMANTIC_VERSION_PATTERN.findall(plan_name))

            network_logger.trace(f"Regex version matches found (prop {proposal_id})", matches=versions)

            version = None
            if versions:
                unique_versions = list(set(versions))
                network_logger.trace(f"Unique version strings found: {unique_versions}")
                network_repo_semver_tags = get_network_repo_semver_tags(network, network_repo_url)
                network_logger.trace(f"Repo tags for semver check (prop {proposal_id})", tags=network_repo_semver_tags)
                version = find_best_semver_for_versions(network, unique_versions, network_repo_semver_tags)
                network_logger.trace(f"Best semver found (prop {proposal_id})", best_version=version)
            else:
                network_logger.trace(f"No regex version matches in content dump or plan name (prop {proposal_id})")

            try:
                height = int(plan.get("height", 0))
            except ValueError:
                height = 0
                network_logger.trace(f"Could not parse height from plan (prop {proposal_id})", plan_height=plan.get("height"))

            if version and height > 0:
                network_logger.trace(f"Returning valid upgrade found in proposal {proposal_id}", name=plan_name, version=version, height=height)
                return plan_name, version, height
    
    # For v1beta1 proposals
    content = proposal.get("content", {})
    proposal_type = content.get("@type")
    if (
        proposal_type == "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal" or
        proposal_type == '/cosmos.upgrade.v1beta1.MsgSoftwareUpgrade'
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

        if version and height > 0:
            return plan_name, version, height
    
    return None, None, None

def fetch_proposals_by_status(rest_url, status, network, network_logger):
    """
    Fetch all proposals with a given status
    
    Args:
        rest_url: REST API endpoint
        status: Status code ("2" for active, "3" for passed)
        network: Network name
        network_logger: Logger instance
        
    Returns:
        List of proposals or empty list on error
    """
    all_proposals = []
    next_key = None

    while True:
        try:
            query_params = {"proposal_status": status}
            if next_key:
                query_params["pagination.key"] = next_key

            response = requests.get(
                f"{rest_url}/cosmos/gov/v1/proposals", params=query_params, verify=False
            )

            if response.status_code == 501:
                network_logger.trace(f"Gov v1 endpoint not implemented (501) for status {status}")
                break

            response.raise_for_status()
            data = response.json()
            
            page_proposals = data.get("proposals", [])
            if page_proposals:
                all_proposals.extend(page_proposals)

            next_key = data.get("pagination", {}).get("next_key")
            if not next_key:
                break

            sleep(0.2)

        except requests.RequestException as e:
            status_code_http = e.response.status_code if e.response is not None else "N/A"
            network_logger.trace(
                f"RequestException during proposal fetch (status {status})",
                server=rest_url,
                status_code=status_code_http,
                error=str(e),
            )
            return []
        except Exception as e:
            network_logger.error(
                f"Unhandled error during proposal fetch (status {status})",
                server=rest_url,
                error=str(e),
                error_type=type(e).__name__,
                trace=traceback.format_exc()
            )
            return []
            
    return all_proposals

def fetch_proposals_beta1_by_status(rest_url, status, network, network_logger):
    """Fetch all proposals with a given status using v1beta1 endpoint"""
    try:
        response = requests.get(
            f"{rest_url}/cosmos/gov/v1beta1/proposals?proposal_status={status}", verify=False
        )

        if response.status_code == 501:
            return []

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
        return data.get("proposals", [])
        
    except RequiresGovV1Exception:
        raise
    except Exception as e:
        network_logger.trace(f"Error fetching v1beta1 proposals: {str(e)}")
        return []
