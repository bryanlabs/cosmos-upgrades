import requests
import urllib.parse
from datetime import datetime, timedelta
from termcolor import colored
import pyshorteners
import pytz
from dateutil import tz
import json


def load_config():
    with open("config.json", "r") as f:
        return json.load(f)


def get_events_from_api():
    base_url = "https://cosmos-upgrades.apis.defiantlabs.net"
    config = load_config()
    networks = config["networks"]

    events = []

    for type, network_list in networks.items():
        response = requests.get(f"{base_url}/{type}")
        data = response.json()

        for network in network_list.split():
            for item in data:
                if item["network"] == network and item["upgrade_found"]:
                    events.append(item)
    return events


def create_google_calendar_event(event_data):
    event_title = f"{event_data['network']} - {event_data['type']} - {event_data['version']}"
    details_url = f"https://www.mintscan.io/{event_data['network']}/blocks/{event_data['upgrade_block_height']}"

    start_datetime = datetime.strptime(event_data["estimated_upgrade_time"], "%Y-%m-%dT%H:%M:%S.%f").replace(
        tzinfo=pytz.utc
    )
    local_datetime = start_datetime.astimezone(tz.tzlocal())  # Using dateutil's tzlocal

    end_datetime = start_datetime + timedelta(minutes=30)

    start_time_str = start_datetime.strftime("%Y%m%dT%H%M%SZ")
    end_time_str = end_datetime.strftime("%Y%m%dT%H%M%SZ")

    google_cal_url = f"https://www.google.com/calendar/render?action=TEMPLATE&text={urllib.parse.quote(event_title)}&dates={start_time_str}/{end_time_str}&details={urllib.parse.quote(details_url)}&sf=true&output=xml"

    s = pyshorteners.Shortener()
    short_google_cal_url = s.tinyurl.short(google_cal_url)

    print("\nEvent:", colored(event_title, "cyan"))
    print(
        "Time (UTC):",
        colored(start_datetime.strftime("%Y-%m-%d %H:%M:%S"), "yellow"),
        "(Local Time:",
        colored(local_datetime.strftime("%Y-%m-%d %H:%M:%S %Z"), "magenta"),
        ")",
    )
    print("Details:", colored(details_url, "green"))
    print("Google Calendar Link:", colored(short_google_cal_url, "blue"))
    print("-" * 80)  # This adds a separator line for better visibility.


def main():
    events = get_events_from_api()

    for event in events:
        create_google_calendar_event(event)


if __name__ == "__main__":
    main()
