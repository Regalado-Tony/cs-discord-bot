import discord
import requests
import json
import os
import time
import re
from collections import Counter, defaultdict

# =========================================================
# SETTINGS
# =========================================================

DISCORD_TOKEN_FILE = "Bot Token.txt"
TEAM_ALIASES_FILE = "team_aliases.json"

BO3_API_BASE = "https://api.bo3.gg/api/v1"

ALLOWED_CHANNEL_ID = 1479321291076014100

REQUEST_TIMEOUT = 25
REQUEST_DELAY_SECONDS = 1.0

MAX_LAST_MATCHES = 20

TEAM_CACHE_TTL = 60 * 60 * 18  # 18 hours
HEAVY_COMMAND_COOLDOWN = 120

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

CORE_MAPS = [
    "Ancient",
    "Anubis",
    "Inferno",
    "Mirage",
    "Dust2",
    "Nuke",
    "Overpass"
]

MAP_NAME_FIXES = {
    "de_dust2": "Dust2",
    "de_inferno": "Inferno",
    "de_mirage": "Mirage",
    "de_overpass": "Overpass",
    "de_nuke": "Nuke",
    "de_ancient": "Ancient",
    "de_anubis": "Anubis",
    "de_train": "Train",
    "de_vertigo": "Vertigo"
}

# =========================================================
# DISCORD SETUP
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# =========================================================
# RUNTIME MEMORY (CACHES)
# =========================================================

team_cache = {}
game_stats_cache = {}
player_rows_cache = {}
channel_context = {}
heavy_cooldown_until = {}
last_request_time = 0

# =========================================================
# FILE HELPERS
# =========================================================

def read_token(filename):
    if not os.path.exists(filename):
        raise FileNotFoundError(f"Missing file: {filename}")

    with open(filename, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_team_aliases():
    if not os.path.exists(TEAM_ALIASES_FILE):
        return {}

    with open(TEAM_ALIASES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


TEAM_ALIASES = load_team_aliases()

# =========================================================
# BASIC HELPERS
# =========================================================

def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def title_map(map_name):
    key = (map_name or "").lower().strip()
    if key in MAP_NAME_FIXES:
        return MAP_NAME_FIXES[key]
    return (map_name or "").title()


def clamp_last_n(n):
    return max(1, min(n, MAX_LAST_MATCHES))


def now():
    return int(time.time())


def chunk_text(text, limit=1900):
    lines = text.split("\n")
    chunks = []
    current = []

    for line in lines:
        if len("\n".join(current + [line])) > limit:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        chunks.append("\n".join(current))

    return chunks

# =========================================================
# REQUEST THROTTLING
# =========================================================

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://bo3.gg",
    "Accept-Language": "en-US,en;q=0.9"
})


def safe_sleep():
    global last_request_time

    elapsed = time.time() - last_request_time
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)

    last_request_time = time.time()


def get_json(url):
    for attempt in range(3):
        try:
            safe_sleep()
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            print("GET", url, r.status_code)

            if r.status_code >= 500:
                time.sleep(2)
                continue

            r.raise_for_status()
            return r.json()

        except requests.exceptions.RequestException as e:
            print("API error:", e)

            if attempt == 2:
                return None

            time.sleep(2)

    return None

# =========================================================
# COOLDOWN HELPERS
# =========================================================

def set_heavy_cooldown(channel_id):
    heavy_cooldown_until[channel_id] = now() + HEAVY_COMMAND_COOLDOWN


def cooldown_remaining(channel_id):
    return max(0, heavy_cooldown_until.get(channel_id, 0) - now())


def is_heavy_blocked(channel_id):
    return cooldown_remaining(channel_id) > 0

# =========================================================
# TEAM RESOLUTION
# =========================================================

def resolve_team(team_name):
    key = norm(team_name)

    for _, data in TEAM_ALIASES.items():
        aliases = data.get("aliases", [])

        for name in [data["name"]] + aliases:
            if norm(name) == key:
                return {
                    "team_id": int(data["team_id"]),
                    "slug": data["slug"],
                    "name": data["name"]
                }

    return None

# =========================================================
# BO3 API CALLS
# =========================================================

def api_recent_matches(team_id):
    url = (
        f"{BO3_API_BASE}/matches"
        f"?page[offset]=0"
        f"&page[limit]=40"
        f"&sort=-start_date"
        f"&filter[matches.status][in]=current,upcoming,finished,defwin"
        f"&filter[matches.team_ids][overlap]={team_id}"
        f"&filter[matches.discipline_id][eq]=1"
        f"&with=teams,tournament,ai_predictions,games,match_maps"
    )

    data = get_json(url)

    if not data:
        return []

    if isinstance(data, list):
        return data

    return data.get("results", [])


def api_roster(match_slug):
    url = f"{BO3_API_BASE}/matches/{match_slug}/game_steam_profiles"

    data = get_json(url)

    if not data:
        return []

    if isinstance(data, list):
        return data

    return data.get("results", [])


def api_game_stats(game_id):
    if not game_id:
        return []

    if game_id in game_stats_cache:
        return game_stats_cache[game_id]

    url = f"{BO3_API_BASE}/games/{game_id}/players_stats"

    data = get_json(url)

    if not data:
        return []

    if isinstance(data, list):
        results = data
    else:
        results = data.get("results", [])

    game_stats_cache[game_id] = results
    return results

# =========================================================
# TEAM SAMPLE BUILDER
# =========================================================

def build_team_sample(team, last_n):
    team_id = team["team_id"]
    cached = team_cache.get(team_id)

    if cached:
        if now() - cached["timestamp"] < TEAM_CACHE_TTL:
            if cached["map_sample_size"] >= last_n:
                return cached

    recent_matches = api_recent_matches(team_id)

    if not recent_matches:
        raise RuntimeError("No recent matches found")

    selected_map_entries = []
    used_series = []

    ban_counter = Counter()
    pick_counter = Counter()

    for match in recent_matches:
        match_maps = match.get("match_maps", [])
        games = match.get("games", [])

        if not match_maps or not games:
            continue

        veto = {"ban": None, "pick": None}

        for entry in match_maps:
            order = entry.get("order")
            if order is None or order > 4:
                continue

            if entry.get("team_id") != team_id:
                continue

            map_name = title_map(entry.get("maps", {}).get("map_name"))
            if not map_name:
                continue

            if entry.get("choice_type") == 2:
                veto["ban"] = map_name
                ban_counter[map_name] += 1

            if entry.get("choice_type") == 1:
                veto["pick"] = map_name
                pick_counter[map_name] += 1

        series_games = []
        for g in games:
            map_name = title_map(g.get("map_name"))
            game_id = g.get("id")

            if not map_name or not game_id:
                continue

            series_games.append(
                {
                    "map": map_name,
                    "game_id": game_id,
                }
            )

        if not series_games:
            continue

        used_series.append(match)

        for g in series_games:
            selected_map_entries.append(
                {
                    "match_id": match.get("id"),
                    "start_date": match.get("start_date"),
                    "games": [g],
                    "veto": veto,
                }
            )

            if len(selected_map_entries) >= last_n:
                break

        if len(selected_map_entries) >= last_n:
            break

    if not selected_map_entries:
        raise RuntimeError("No usable recent maps found")

    latest_match = used_series[0]
    roster_data = api_roster(latest_match["slug"])

    roster = []
    seen = set()

    for row in roster_data:
        steam_profile = row.get("steam_profile", {})
        player = steam_profile.get("player", {})

        if not player:
            continue

        if player.get("team_id") != team_id:
            continue

        name = player.get("nickname") or steam_profile.get("nickname")
        slug = player.get("slug")

        if not name or not slug:
            continue

        if slug in seen:
            continue

        seen.add(slug)
        roster.append(
            {
                "name": name,
                "slug": slug
            }
        )

    roster = roster[:5]

    sample = {
        "timestamp": now(),
        "max_matches_cached": len(used_series),
        "map_sample_size": len(selected_map_entries),
        "series_sample_size": len(used_series),
        "matches": selected_map_entries,
        "ban_counter": dict(ban_counter),
        "pick_counter": dict(pick_counter),
        "roster": roster
    }

    team_cache[team_id] = sample
    return sample


# =========================================================
# PLAYER MAP ROW BUILDER
# =========================================================

def build_player_map_rows(player_slug, matches):

    cache_key = (player_slug, tuple(m.get("match_id") for m in matches), len(matches))

    if cache_key in player_rows_cache:
        return player_rows_cache[cache_key]

    player_map_rows = defaultdict(list)

    for match in matches:

        for g in match["games"]:

            game_id = g.get("game_id")
            map_name = g.get("map")

            if not game_id or not map_name:
                continue

            stats = api_game_stats(game_id)

            for row in stats:

                row_slug = (
                    row.get("player_slug")
                    or row.get("slug")
                    or row.get("player", {}).get("slug")
                    or row.get("steam_profile", {}).get("player", {}).get("slug")
                )

                if row_slug != player_slug:
                    continue

                kills = int(row.get("kills", 0) or 0)
                hs = int(row.get("headshots", 0) or 0)

                player_map_rows[map_name].append(
                    {
                        "kills": kills,
                        "hs": hs
                    }
                )

    player_rows_cache[cache_key] = player_map_rows
    return player_map_rows


# =========================================================
# MAP AVERAGES
# =========================================================

def compute_map_averages(player_map_rows):
    rows = []

    for map_name, games in player_map_rows.items():
        maps_played = len(games)
        if maps_played == 0:
            continue

        avg_kills = sum(x["kills"] for x in games) / maps_played
        avg_hs = sum(x["hs"] for x in games) / maps_played

        rows.append((map_name, maps_played, avg_kills, avg_hs))

    rows.sort(key=lambda x: (-x[1], -x[2]))
    return rows


# =========================================================
# LINE HIT CALCULATIONS
# =========================================================

def compute_line_hits(player_map_rows, stat_key, line):
    total_hits = 0
    total_maps = 0
    by_map = []

    for map_name, games in player_map_rows.items():
        values = [g[stat_key] for g in games]
        hits = sum(1 for v in values if v > line)

        total_hits += hits
        total_maps += len(values)

        by_map.append((map_name, hits, len(values)))

    by_map.sort(key=lambda x: (-x[2], x[0]))
    return total_hits, total_maps, by_map


# =========================================================
# VS PREDICTION
# =========================================================

def predict_veto(team1_sample, team2_sample):
    t1_bans = Counter(team1_sample["ban_counter"])
    t1_picks = Counter(team1_sample["pick_counter"])
    t2_bans = Counter(team2_sample["ban_counter"])
    t2_picks = Counter(team2_sample["pick_counter"])

    def top(counter):
        if not counter:
            return None
        return counter.most_common(1)[0][0]

    return {
        "ban1": top(t1_bans),
        "ban2": top(t2_bans),
        "pick1": top(t1_picks),
        "pick2": top(t2_picks)
    }


# =========================================================
# TOP PLAYERS ON MAP
# =========================================================

def top_players_for_map(sample, map_name):
    matches = sample["matches"]
    results = []

    for p in sample["roster"]:
        rows = build_player_map_rows(p["slug"], matches)
        games = rows.get(map_name, [])

        if not games:
            continue

        maps_played = len(games)
        avg_kills = sum(x["kills"] for x in games) / maps_played
        avg_hs = sum(x["hs"] for x in games) / maps_played

        results.append(
            (
                p["name"],
                maps_played,
                avg_kills,
                avg_hs
            )
        )

    results.sort(key=lambda x: (-x[2], -x[1]))
    return results[:2]


# =========================================================
# OUTPUT FORMATTERS
# =========================================================

def format_team_output(team_name, sample):

    lines = []
    lines.append(f"{team_name} Last {sample['map_sample_size']} Maps")
    lines.append("")

    bans = Counter(sample["ban_counter"])
    picks = Counter(sample["pick_counter"])
    series_total = sample["series_sample_size"]

    ban_maps = [(m, bans[m]) for m in CORE_MAPS if bans.get(m, 0) > 0]
    pick_maps = [(m, picks[m]) for m in CORE_MAPS if picks.get(m, 0) > 0]
    ignored_maps = [
        m for m in CORE_MAPS
        if bans.get(m, 0) == 0 and picks.get(m, 0) == 0
    ]

    ban_maps.sort(key=lambda x: -x[1])
    pick_maps.sort(key=lambda x: -x[1])

    if ban_maps:
        lines.append("Banned")
        for m, v in ban_maps:
            lines.append(f"{m:<10} {v}/{series_total}")
        lines.append("")

    if pick_maps:
        lines.append("Picked")
        for m, v in pick_maps:
            lines.append(f"{m:<10} {v}/{series_total}")
        lines.append("")

    if ignored_maps:
        lines.append("Ignored")
        for m in ignored_maps:
            lines.append(m)
        lines.append("")

    lines.append("Roster")

    for i, p in enumerate(sample["roster"], 1):
        lines.append(f"{i} {p['name']}")

    return "\n".join(lines)


def format_expand_output(player_name, rows, map_sample_size):
    lines = [f"{player_name} Last {map_sample_size} Maps", ""]

    for map_name, maps_played, avg_kills, avg_hs in rows:
        lines.append(f"{map_name:<10} {maps_played} maps {avg_kills:.1f} kills {avg_hs:.1f} hs")

    return "\n".join(lines)


def format_line_output(player_name, stat, line, hits, total, by_map):
    lines = []
    lines.append(f"{player_name} {stat.upper()} Line {line}")
    lines.append("")
    lines.append(f"Overall {hits}/{total}")
    lines.append("")
    lines.append("By Map")

    for m, h, t in by_map:
        lines.append(f"{m:<10} {h}/{t}")

    return "\n".join(lines)

# =========================================================
# COMMAND PARSING
# =========================================================

def parse_team_last(cmd):

    m = re.match(r"^!(.+?)\s+last\s+(\d+)$", cmd, re.IGNORECASE)

    if not m:
        return None

    team = m.group(1).strip()
    n = clamp_last_n(int(m.group(2)))

    return team, n


def parse_expand(cmd):

    m = re.match(r"^!expand\s+(\d+)$", cmd, re.IGNORECASE)

    if not m:
        return None

    return int(m.group(1))


def parse_line(cmd):

    m = re.match(r"^!([^\!]+?)\s+(kills|hs)\s+([0-9]+(?:\.[0-9]+)?)$", cmd, re.IGNORECASE)

    if not m:
        return None

    player = m.group(1).strip()
    stat = m.group(2).lower()
    line = float(m.group(3))

    return player, stat, line


def parse_vs(cmd):

    m = re.match(r"^!(.+?)\s+vs\s+(.+?)\s+last\s+(\d+)$", cmd, re.IGNORECASE)

    if not m:
        return None

    t1 = m.group(1).strip()
    t2 = m.group(2).strip()
    n = clamp_last_n(int(m.group(3)))

    return t1, t2, n


# =========================================================
# MESSAGE SENDER
# =========================================================

async def send_text(message, text):

    for chunk in chunk_text(text):
        await message.channel.send(f"```{chunk}```")


# =========================================================
# COMMAND EXECUTION
# =========================================================

async def run_team_last(message, team_name, last_n):

    team = resolve_team(team_name)

    if not team:
        await message.channel.send("Team not found in aliases.")
        return

    team_id = team["team_id"]
    cached = team_cache.get(team_id)

    needs_fetch = True

    if cached:
        if now() - cached["timestamp"] < TEAM_CACHE_TTL:
            if cached["map_sample_size"] >= last_n:
                needs_fetch = False

    if needs_fetch:
        if is_heavy_blocked(message.channel.id):
            remain = cooldown_remaining(message.channel.id)
            await message.channel.send(f"Heavy command cooldown active. Wait {remain}s.")
            return

        set_heavy_cooldown(message.channel.id)

    sample = build_team_sample(team, last_n)

    channel_context[message.channel.id] = {
        "team": team,
        "sample": sample
    }

    output = format_team_output(team["name"], sample)

    await send_text(message, output)


async def run_expand(message, index):

    ctx = channel_context.get(message.channel.id)

    if not ctx:
        await message.channel.send("Run a team command first.")
        return

    sample = ctx["sample"]
    roster = sample["roster"]

    if index < 1 or index > len(roster):
        await message.channel.send("Roster slot must be 1-5.")
        return

    player = roster[index - 1]

    rows = build_player_map_rows(player["slug"], sample["matches"])
    avg = compute_map_averages(rows)

    output = format_expand_output(player["name"], avg, sample["map_sample_size"])

    await send_text(message, output)


async def run_player_line(message, player_name, stat, line):

    ctx = channel_context.get(message.channel.id)

    if not ctx:

        await message.channel.send("Run a team command first.")
        return

    sample = ctx["sample"]

    player_slug = None
    player_real_name = None

    for p in sample["roster"]:

        if norm(p["name"]) == norm(player_name):

            player_slug = p["slug"]
            player_real_name = p["name"]

    if not player_slug:

        await message.channel.send("Player not found in current roster.")
        return

    rows = build_player_map_rows(player_slug, sample["matches"])

    stat_key = "kills" if stat == "kills" else "hs"

    hits, total, by_map = compute_line_hits(rows, stat_key, line)

    output = format_line_output(player_real_name, stat, line, hits, total, by_map)

    await send_text(message, output)


async def run_vs(message, team1_name, team2_name, last_n):

    t1 = resolve_team(team1_name)
    t2 = resolve_team(team2_name)

    if not t1 or not t2:

        await message.channel.send("Team not found.")
        return

    needs_fetch = False

    for team in [t1, t2]:

        cached = team_cache.get(team["team_id"])

        if not cached:

            needs_fetch = True

        else:

            if cached["max_matches_cached"] < last_n:
                needs_fetch = True

            if now() - cached["timestamp"] > TEAM_CACHE_TTL:
                needs_fetch = True

    if needs_fetch:

        if is_heavy_blocked(message.channel.id):

            remain = cooldown_remaining(message.channel.id)

            await message.channel.send(
                f"Heavy command cooldown active. Wait {remain}s."
            )
            return

        set_heavy_cooldown(message.channel.id)

    s1 = build_team_sample(t1, last_n)
    s2 = build_team_sample(t2, last_n)

    veto = predict_veto(s1, s2)

    lines = []

    lines.append(f"{t1['name']} vs {t2['name']} Last {last_n}")
    lines.append("")
    lines.append("Likely Bans")
    lines.append(f"{t1['name']} -> {veto['ban1']}")
    lines.append(f"{t2['name']} -> {veto['ban2']}")
    lines.append("")
    lines.append("Likely Picks")
    lines.append(f"{t1['name']} -> {veto['pick1']}")
    lines.append(f"{t2['name']} -> {veto['pick2']}")
    lines.append("")

    for map_name in [veto["pick1"], veto["pick2"]]:

        if not map_name:
            continue

        lines.append(map_name)
        lines.append("")

        t1_top = top_players_for_map(s1, map_name)
        t2_top = top_players_for_map(s2, map_name)

        lines.append(t1["name"])

        for p in t1_top:

            lines.append(f"{p[0]:<12} {p[1]} maps {p[2]:.1f} kills {p[3]:.1f} hs")

        lines.append("")

        lines.append(t2["name"])

        for p in t2_top:

            lines.append(f"{p[0]:<12} {p[1]} maps {p[2]:.1f} kills {p[3]:.1f} hs")

        lines.append("")
        lines.append("")

    await send_text(message, "\n".join(lines))


# =========================================================
# DISCORD EVENTS
# =========================================================

@client.event
async def on_ready():

    print(f"Logged in as {client.user}")
    print(f"Bot restricted to channel {ALLOWED_CHANNEL_ID}")


@client.event
async def on_message(message):

    if message.author == client.user:
        return

    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    cmd = message.content.strip()

    if not cmd.startswith("!"):
        return

    expand = parse_expand(cmd)

    if expand is not None:
        await run_expand(message, expand)
        return

    vs = parse_vs(cmd)

    if vs:
        await run_vs(message, vs[0], vs[1], vs[2])
        return

    team_last = parse_team_last(cmd)

    if team_last:
        await run_team_last(message, team_last[0], team_last[1])
        return

    line = parse_line(cmd)

    if line:
        await run_player_line(message, line[0], line[1], line[2])
        return


# =========================================================
# START BOT
# =========================================================

if __name__ == "__main__":

    token = read_token(DISCORD_TOKEN_FILE)

    client.run(token)