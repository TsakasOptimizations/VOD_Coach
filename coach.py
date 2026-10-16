"""VOD analysis: split the match into segments, have Gemini watch each one, merge into one report."""
import json, math, os, re, subprocess, tempfile, time, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Literal, Optional

import httpx
import imageio_ffmpeg
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # the windowed .exe would otherwise flash a console for ffmpeg
HERE = Path(__file__).parent
# Low media resolution = ~100 tokens per video second, so a 25 min part is ~160k tokens: under the free 250k tokens/minute.
SEGMENT_SEC = 25 * 60
LOW_RES = types.MediaResolution.MEDIA_RESOLUTION_LOW


class Moment(BaseModel):
    time: str  # MM:SS inside the clip
    round: Optional[int]
    category: str
    severity: Literal["good", "minor", "major", "critical"]
    title: str
    what_happened: str
    better: str


class SegmentReport(BaseModel):
    map: str
    context: str  # game state so far, handed to the next segment
    moments: list[Moment]


class Issue(BaseModel):
    issue: str
    advice: str


class Summary(BaseModel):
    overview: str
    top_issues: list[Issue]
    strengths: list[str]


def duration(path):
    out = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True, errors="ignore", creationflags=NO_WINDOW).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    if not m:
        raise ValueError("Could not read video duration - is this a video file?")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def to_seconds(ts):
    secs = 0
    for part in ts.strip().split(":"):
        secs = secs * 60 + float(part)
    return secs


def cut_segment(src, start, length, dst, height):
    # Low fps keeps uploads small; Gemini only samples 1 fps anyway. Audio kept for footsteps/comms/pings.
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-t", str(length), "-i", src,
                    "-vf", f"scale=-2:{height},fps=5", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                    "-c:a", "aac", "-b:a", "64k", dst], check=True, creationflags=NO_WINDOW)


# --- Free-tier quota tracking. Google has no API for "requests left", so we count our own requests per Pacific day
# and mark a model as used up when Google says so (covers requests made outside the app).
FREE_RPD = 20  # ponytail: free-tier requests/day per Flash model, read from aistudio.google.com/rate-limit (Oct 2026)
USAGE_FILE = Path.home() / ".vodcoach_usage.json"
PT = ZoneInfo("America/Los_Angeles")  # Google resets daily quotas at midnight Pacific


def _usage():
    today = datetime.now(PT).date().isoformat()
    try:
        u = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        u = {}
    return u if u.get("day") == today else {"day": today, "used": {}}


def _bump(model, n=None):
    u = _usage()
    u["used"][model] = FREE_RPD if n is None else u["used"].get(model, 0) + n
    USAGE_FILE.write_text(json.dumps(u), encoding="utf-8")


def requests_left(model):
    return max(0, FREE_RPD - _usage()["used"].get(model, 0))


def requests_per_game(seconds):
    return math.ceil(max(seconds, 1) / SEGMENT_SEC) + 1  # one per video part + the summary


def reset_in_seconds():
    now = datetime.now(PT)
    return int((datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), PT) - now).total_seconds())


def generate(client, progress, models, **kw):
    """generate_content on models[0]. Retries while Google is overloaded or the per-minute limit is hit;
    when a model's daily quota runs out, moves on to the next model in the list (models is mutated)."""
    attempt = 0
    while True:
        while models and requests_left(models[0]) == 0:
            models.pop(0)
        if not models:
            h, m = divmod(reset_in_seconds() // 60, 60)
            raise RuntimeError(f"Daily free limit reached for every coach. It resets in {h}h {m}m (midnight Pacific).")
        try:
            resp = client.models.generate_content(model=models[0], **kw)
            _bump(models[0], 1)
            return resp
        except (errors.ServerError, httpx.TransportError):  # Google overloaded, or the connection dropped / timed out
            attempt += 1
            if attempt == 6:
                raise RuntimeError("Gemini is overloaded right now. Try again in a few minutes or pick another coach.")
            progress("Coach is still watching...")
            time.sleep(15 * attempt)
        except errors.ClientError as e:
            if e.code != 429:
                raise
            if "PerDay" in str(e):
                _bump(models[0])  # used up today, even if we didn't count it all ourselves
                progress("This coach hit its daily free limit, switching to the next coach...")
            else:
                progress("Per-minute limit hit, waiting 60s...")
                time.sleep(60)


def get_json(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "vodcoach"}), timeout=30))


CACHE_DIR = Path.home() / ".vodcoach_cache"  # writable even when running as a packaged .exe


def cached(file, fetch):
    """Fresh data when online (and refresh the cache), else the last cache, else the copy shipped with the app."""
    try:
        data = fetch()
        CACHE_DIR.mkdir(exist_ok=True)
        (CACHE_DIR / file).write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        return data
    except Exception:
        for f in (CACHE_DIR / file, HERE / file):
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return {}


def valorant_kits():
    """{agent: {"icon": url, "kit": [[key, ability, description], ...]}} from valorant-api.com."""
    keys = {"Grenade": "C", "Ability1": "Q", "Ability2": "E", "Ultimate": "X", "Passive": "Passive"}
    data = get_json("https://valorant-api.com/v1/agents?isPlayableCharacter=true")["data"]
    return {a["displayName"]: {"icon": a["displayIcon"],
                               "kit": [[keys.get(ab["slot"], ab["slot"]), ab["displayName"], ab["description"]]
                                       for ab in a["abilities"]]} for a in data}


def lol_kits():
    """{champion: {"icon": url, "kit": [[key, ability, description], ...]}} from Riot's Data Dragon."""
    v = get_json("https://ddragon.leagueoflegends.com/api/versions.json")[0]
    data = get_json(f"https://ddragon.leagueoflegends.com/cdn/{v}/data/en_US/championFull.json")["data"]
    clean = lambda s: re.sub(r"<[^>]+>", " ", s).replace("  ", " ").strip()
    return {c["name"]: {"icon": f"https://ddragon.leagueoflegends.com/cdn/{v}/img/champion/{c['image']['full']}",
                        "kit": [["Passive", c["passive"]["name"], clean(c["passive"]["description"])]] +
                               [[k, s["name"], clean(s["description"])] for k, s in zip("QWER", c["spells"])]}
            for c in data.values()}


def valorant_ranks():
    tiers = get_json("https://valorant-api.com/v1/competitivetiers")["data"][-1]["tiers"]
    return [{"name": t["tierName"].title(), "icon": t["smallIcon"]} for t in tiers if t["tier"] >= 3 and t["smallIcon"]]


LOL_ASSETS = "https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-mini-crests/"


def lol_ranks():
    tiers = [f"{t} {d}" for t in ("Iron", "Bronze", "Silver", "Gold", "Platinum", "Emerald", "Diamond")
             for d in ("IV", "III", "II", "I")] + ["Master", "Grandmaster", "Challenger"]
    return [{"name": t, "icon": LOL_ASSETS + t.split()[0].lower() + ".svg"} for t in tiers]


GAMES = {
    "valorant": {
        "title": "VALORANT",
        "kits": lambda: cached("valorant.json", valorant_kits),
        "ranks": lambda: cached("valorant_ranks.json", valorant_ranks) or [],
        "height": 720,
        "categories": "death, ability, positioning, crosshair, utility, economy, trade, comms, good_play",
        "who": lambda p: f"playing {p['agent']}, currently {p['rank']}",
        "focus_hint": lambda p: f"""- what_happened: what you actually see on screen (position, ability used, enemies, minimap, kill feed).
- better: the specific alternative - where/when to use the ability instead, where to stand, what to do with the round.
Be specific to {p['agent']}'s kit (e.g. "you used your dash to enter site head-first; save it to escape after the first kill").
Cover deaths, ability usage (wasted / mistimed / unused at round end), positioning, crosshair placement,
trades, economy buys, and rotations. round: the round number.
Ignore menus, agent select and dead-time spectating.
map: the map name.
context: one or two sentences of map, side, score and the player's patterns so far, for the next clip's coach.""",
        "summary_unit": "rounds",
    },
    "lol": {
        "title": "League of Legends",
        "kits": lambda: cached("lol.json", lol_kits),
        "ranks": lol_ranks,
        "height": 1080,  # ponytail: minimap is tiny; if Gemini still misreads it, upload a cropped+enlarged minimap too
        "categories": "cs_waves, trading, recall, vision, map_awareness, objectives, rotation, teamfight, itemization, death, good_play",
        "who": lambda p: f"playing {p['agent']} {p.get('role', '')}, currently {p['rank']}"
                         + (f", lane opponent {p['opponent']}" if p.get("opponent") else ""),
        "focus_hint": lambda p: f"""- what_happened: what you actually see (CS/gold, wave state, HP/mana, minimap, enemy positions, items, timers).
- better: the specific alternative - e.g. freeze/slow push/crash the wave, back now, ward this bush, rotate to drake,
  don't facecheck, trade when their key ability is down, buy this item instead.
Be specific to {p['agent']}'s kit and power spikes as a {p.get('role', 'player')}.
Cover CS and wave management, trading in lane, recall timing, vision/warding, map awareness (missing enemies, jungler
tracking via minimap), objective setup (grubs, herald, dragon, baron), rotations, teamfight positioning and target
selection, itemization, and deaths. round: always null.
Ignore champ select, loading screen, shop browsing and dead-time spectating.
map: the matchup, e.g. "Ahri vs Zed (mid)".
context: one or two sentences of game time, gold/kill state, objectives taken and the player's patterns so far, for the next clip's coach.""",
        "summary_unit": "games",
    },
}


def find(kits, name):
    """Kit key for a typed name, ignoring case ("ahri" -> "Ahri"), else None."""
    return next((k for k in kits if name and k.lower() == name.strip().lower()), None)


def kits_text(game, kits, p, everyone=True):
    fmt = lambda name: "\n".join(f"  {key} - {ab}: {desc}" for key, ab, desc in kits[name]["kit"])
    mine = find(kits, p["agent"])
    out = f"\nThe player's kit ({mine}) - use these exact ability names and keys:\n{fmt(mine)}\n" if mine else ""
    # Valorant: every agent fits in the prompt. League has ~170 champs, so only the lane opponent.
    others = sorted(k for k in kits if k != mine) if game == "valorant" and everyone else \
        [n for n in [find(kits, p.get("opponent"))] if n]
    if others:
        out += "\nOther kits, to identify teammate/enemy abilities correctly:\n" + "\n".join(f"{n}:\n{fmt(n)}" for n in others) + "\n"
    return out


FEEDBACK_FILE = Path.home() / ".vodcoach_feedback.json"


def load_feedback():
    try:
        return json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def feedback_text(game, agent):
    # ponytail: newest 40 corrections for this game, this agent first; summarise with the model if the list grows huge
    fb = [f for f in load_feedback() if f.get("game", "valorant") == game][-40:]
    fb.sort(key=lambda f: f["agent"].lower() != agent.strip().lower())
    if not fb:
        return ""
    lines = "\n".join(f"- [{f['agent']}] You said: {f['note']}" + (f"\n  Player's reason: {f['reason']}" if f["reason"] else "")
                      for f in fb)
    return f"""
In past reviews the player DISAGREED with these notes of yours. Learn from them: don't repeat the same kind of
advice in the same situations, and adopt their reasoning where it makes sense (they know their team's strategy and comms).
{lines}
"""


def prompt(game, p, offset, prev_context, kits):
    g = GAMES[game]
    return f"""You are an elite {g['title']} coach reviewing a player's own POV recording.
Player: {g['who'](p)}. Pitch advice at the level that will get them to the next rank.
{kits_text(game, kits, p)}
Never invent ability names or effects - only use the kits above.
{feedback_text(game, p['agent'])}
{f"The player asked you to focus on: {p['focus']}" if p.get('focus') else ""}
This clip starts {int(offset // 60)} minutes into the full recording.
{f"What happened before this clip: {prev_context}" if prev_context else "This is the start of the recording (may include menus / loading)."}

Watch the whole clip and list concrete, timestamped coaching moments. For each:
- time: MM:SS relative to THIS clip, at the moment the decision was made (not when they died).
- category: one of {g['categories']}.
{g['focus_hint'](p)}
Include clear good plays too (severity "good", category "good_play") so they know what to keep doing.
Only report what you can actually see; don't invent events."""


def analyze(path, api_key, models, p, progress):
    """models: chosen model first, then fallbacks for when its daily quota runs out.
    p: {game, agent, rank, focus, role?, opponent?} - agent is the champion for League.
    progress(msg, span=None): span = (start, end) share of the whole job (0..1) the step covers."""
    models = list(models)
    game, g = p["game"], GAMES[p["game"]]
    # Without a timeout a dropped connection waits forever. 10 min (ms) is far above a normal reply for a 25 min part.
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=10 * 60 * 1000))
    total = duration(path)
    progress("Loading abilities...")
    kits = g["kits"]()
    canon = lambda n: find(kits, n) or n
    p = {**p, "agent": canon(p["agent"]), "opponent": canon(p.get("opponent", ""))}
    if game != "lol":  # role and lane opponent are League-only; the form keeps the last League values around
        p.update(role="", opponent="")
    moments, contexts, map_name = [], [], ""
    starts = list(range(0, int(total), SEGMENT_SEC))
    units = len(starts) + 0.15  # each part is 1 unit (mostly the watching), the summary is a small extra step
    span = lambda i, a, b: ((i + a) / units, (i + b) / units)
    with tempfile.TemporaryDirectory() as tmp:
        for i, start in enumerate(starts):
            tag = f"Part {i + 1}/{len(starts)}"
            progress(f"{tag}: compressing video...", span(i, 0, .1))
            seg = os.path.join(tmp, f"seg{i}.mp4")
            cut_segment(path, start, SEGMENT_SEC, seg, g["height"])

            progress(f"{tag}: uploading VOD to coach...", span(i, .1, .25))
            f = client.files.upload(file=seg)
            try:
                while f.state.name == "PROCESSING":
                    time.sleep(3)
                    f = client.files.get(name=f.name)
                if f.state.name != "ACTIVE":
                    raise RuntimeError(f"Gemini could not process the video ({f.state.name})")

                progress(f"{tag}: coach is watching...", span(i, .25, 1))
                resp = generate(client, progress, models,
                                contents=[f, prompt(game, p, start, contexts[-1] if contexts else "", kits)],
                                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                                   response_schema=SegmentReport,
                                                                   media_resolution=LOW_RES))
            finally:
                try:
                    client.files.delete(name=f.name)
                except errors.APIError:
                    pass  # not fatal: Gemini auto-deletes uploads after 48h

            rep = resp.parsed or SegmentReport.model_validate_json(resp.text)
            map_name = map_name or rep.map
            contexts.append(rep.context)
            for m in rep.moments:
                d = m.model_dump()
                d["seconds"] = start + to_seconds(m.time)
                moments.append(d)

    progress("Writing summary...", span(len(starts), 0, .15))
    summary = generate(
        client, progress, models,
        contents=f"""You coached a {g['title']} player ({g['who'](p)}) through a full match. Your notes:
{json.dumps([{k: m[k] for k in ("category", "severity", "title", "what_happened", "better")} for m in moments])}
Match flow: {" | ".join(contexts)}
{kits_text(game, kits, p, everyone=False)}
Write an overview, the 3-5 recurring issues that cost them the most {g['summary_unit']} (with concrete drills/habits to fix them), and their strengths.""",
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Summary))
    s = summary.parsed or Summary.model_validate_json(summary.text)

    moments.sort(key=lambda m: m["seconds"])
    return {"video": os.path.basename(path), **{k: p.get(k, "") for k in ("game", "agent", "rank", "role", "opponent")},
            "map": map_name, "model": models[0] if models else "", **s.model_dump(), "moments": moments}


if __name__ == "__main__":
    assert to_seconds("01:30") == 90 and to_seconds("1:02:03") == 3723 and to_seconds("0:05") == 5
    print("ok")
