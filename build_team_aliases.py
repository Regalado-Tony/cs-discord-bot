import requests
import json

API = "https://api.bo3.gg/api/v2/team_rankings"

teams = {}

page = 1
per_page = 50
target = 95

while len(teams) < target:

    url = f"{API}?page={page}&per_page={per_page}&filter[discipline_id][eq]=1"

    r = requests.get(url)
    data = r.json()

    results = data.get("data", [])

    for row in results:

        team = row["team"]

        team_id = team["id"]
        name = team["name"]
        slug = team["slug"]

        key = name.lower().replace(" ", "")

        teams[key] = {
            "team_id": team_id,
            "slug": slug,
            "name": name,
            "aliases": []
        }

        if len(teams) >= target:
            break

    page += 1

with open("team_aliases.json", "w", encoding="utf-8") as f:
    json.dump(teams, f, indent=4)

print(f"Saved {len(teams)} teams to team_aliases.json")