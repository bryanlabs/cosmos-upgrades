declare -A networks=(
  [mainnets]="akash agoric axelar celestia comdex composable cosmoshub crescent cronos dydx evmos injective juno kava kujira neutron noble nolys omniflixhub osmosis quasar quicksilver sei sentinel sommelier stargaze stride terra2 umee"
  [testnets]="cosmoshubtestnet neutrontestnet nobetestnet osmosistestnet seitestnet"
)

# base_url="https://cosmos-upgrades.apis.defiantlabs.net"
base_url="http://localhost:5000"

# Loop over both mainnets and testnets
for type in "${!networks[@]}"; do
  # Construct the jq filter dynamically
  jq_filter='.[] | select(.network | IN('
  for network in ${networks[$type]}; do
    jq_filter+='"'$network'",'
  done
  jq_filter=${jq_filter%,}')) | select(.upgrade_found == true)'

  # Use the constructed filter with curl and jq
  curl -s -X GET \
    -H "Content-Type: application/json" \
    $base_url/"$type" | jq "$jq_filter"
done
