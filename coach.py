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


class CoachError(RuntimeError):
    """A problem the player can act on (limits, Gemini busy, not a video). Shown as is; anything else is a bug."""

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # the windowed .exe would otherwise flash a console for ffmpeg
HERE = Path(__file__).parent
# Each part has to stay under the free 250k tokens/minute. Low media resolution is ~100 tokens per video second (25 min
# part ~160k tokens); medium is ~300 (10 min part ~180k), which League needs to read the minimap and HUD.
LOW_RES = types.MediaResolution.MEDIA_RESOLUTION_LOW
MEDIUM_RES = types.MediaResolution.MEDIA_RESOLUTION_MEDIUM


class Moment(BaseModel):
    time: str  # MM:SS inside the clip
    round: Optional[int]
    category: str
    situation: str  # the facts before the decision; asked for first so the verdict is built on them, not on the outcome
    what_happened: str
    severity: Literal["good", "minor", "major", "critical"]
    title: str
    better: str


class SegmentReport(BaseModel):
    map: str
    context: str  # game state so far, handed to the next segment
    moments: list[Moment]


class Issue(BaseModel):
    issue: str
    advice: str


class Verdict(BaseModel):  # a second look at one note, the way a pro checks their own review before handing it over
    i: int
    category: str
    severity: Literal["good", "minor", "major", "critical"]
    title: str
    better: str


class Summary(BaseModel):
    verdicts: list[Verdict]
    overview: str
    top_issues: list[Issue]
    strengths: list[str]


def duration(path):
    out = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True, errors="ignore", creationflags=NO_WINDOW).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    if not m:
        raise CoachError("Couldn't read this video. Is it a video file?")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def to_seconds(ts):
    secs = 0
    for part in ts.strip().split(":"):
        secs = secs * 60 + float(part)
    return secs


def cut_segment(src, start, length, dst, frame):
    # Low fps keeps uploads small; Gemini only samples 1 fps anyway. Audio kept for footsteps/comms/pings.
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-t", str(length), "-i", src,
                    "-filter_complex", f"[0:v]fps=5,{frame}[v]", "-map", "[v]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "64k", dst],
                   check=True, creationflags=NO_WINDOW)


# League frame: game view, with the minimap and the HUD bar (levels, HP/mana, cooldowns, summoners, items) cut out
# and enlarged next to it, so they're still readable after Gemini shrinks each frame.
# ponytail: crops assume a 16:9 recording with default minimap/HUD placement; make them settings if players differ
LOL_FRAME = ("split=3[a][b][c];[a]scale=960:540[g];[b]crop=iw*0.22:ih*0.4:iw*0.78:ih*0.6,scale=540:540[m];"
             "[c]crop=iw*0.36:ih*0.11:iw*0.28:ih*0.89,scale=1500:258[h];[g][m]hstack[t];[t][h]vstack")


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


def requests_per_game(seconds, game="valorant"):
    return math.ceil(max(seconds, 1) / GAMES[game]["segment"]) + 1  # one per video part + the summary


def reset_in_seconds():
    now = datetime.now(PT)
    return int((datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), PT) - now).total_seconds())


# Models Google still lists but refuses ("no longer available to new users"). Remembered so the coach list hides them.
RETIRED_FILE = Path.home() / ".vodcoach_cache" / "retired_models.json"


def retired():
    try:
        return set(json.loads(RETIRED_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def retire(model):
    RETIRED_FILE.parent.mkdir(exist_ok=True)
    RETIRED_FILE.write_text(json.dumps(sorted(retired() | {model})), encoding="utf-8")


def generate(client, progress, models, **kw):
    """generate_content on models[0]. When a model is overloaded, tries the next one right away (it goes to the back of
    the line); when its daily quota runs out or Google has retired it, drops it. Waits out the per-minute limit.
    models is mutated.
    Progress messages name models by id; the UI swaps in the coach names."""
    attempt, dropped_retired = 0, False
    while True:
        while models and requests_left(models[0]) == 0:
            models.pop(0)
        if not models:
            h, m = divmod(reset_in_seconds() // 60, 60)
            if dropped_retired:
                raise CoachError(f"No coach left to use: the others are retired by Google or out of free reviews today "
                                   f"(those reset in {h}h {m}m, midnight Pacific).")
            raise CoachError(f"Daily free limit reached for every coach. It resets in {h}h {m}m (midnight Pacific).")
        try:
            resp = client.models.generate_content(model=models[0], **kw)
            _bump(models[0], 1)
            return resp
        except (errors.ServerError, httpx.TransportError):  # Google overloaded, or the connection dropped / timed out
            attempt += 1
            if attempt == 1:
                give_up = time.time() + 5 * 60  # overload spells often last a few minutes: ride them out
            if time.time() > give_up:
                raise CoachError("All coaches are overloaded right now. Try again in a few minutes "
                                 "(the parts already reviewed are saved).")
            wait = min(5 * attempt, 60)
            if len(models) > 1:
                progress(f"{models[0]} is busy, switching to {models[1]}...")
                models.append(models.pop(0))
            else:
                progress(f"Coach is busy, retrying in {wait}s...")
            time.sleep(wait)
        except errors.ClientError as e:
            if e.code == 404:  # retired model, e.g. gemini-2.5-flash for keys made after its shutdown
                retire(models[0])
                progress(f"{models[0]} isn't available anymore, switching to the next coach...")
                models.pop(0)
                dropped_retired = True
                continue
            if e.code != 429:
                raise
            if "PerDay" in str(e):
                _bump(models[0])  # used up today, even if we didn't count it all ourselves
                progress(f"{models[0]} hit its daily free limit, switching to the next coach...")
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
        "segment": 25 * 60, "res": LOW_RES, "frame": "scale=-2:720",
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
        "segment": 10 * 60, "res": MEDIUM_RES, "frame": LOL_FRAME,
        "layout": "Each frame shows the game (top left), the minimap enlarged (top right) and the HUD bar enlarged "
                  "(bottom: level, HP/mana, ability and summoner spell cooldowns, items, gold). Read them.",
        "rules": """How a pro coach judges - follow this strictly:
- Judge the decision with what was knowable at that moment, never the outcome. A dive or fight that got a kill can
  still be a bad play (it worked because the enemy misplayed or it was a coin flip). Say so plainly. A death can be correct.
- Before judging a fight, trade, dive, recall or rotation, read and write down: game time; both sides' levels and
  upcoming spikes (level 2/3/6, item completions); HP and mana; which summoner spells and key abilities are down on
  each side; the wave (size, who it pushes towards, crashing or under whose tower); where the enemy jungler was last
  seen and how long ago; vision; gold.
- Dives: was the wave under the tower to tank shots, how many tower shots it took, enemy summoners up, enemy jungler
  location, whether the reward beat the risk. A dive that only worked because the enemy misplayed is not good.
- Also count missed opportunities, not only errors: CS left in waves, wave states wasted before objectives or recalls,
  off-tempo recalls, unspent gold, untracked jungler, free plates or objectives not taken, cooldown windows not punished.
- better is the exact alternative with timing, e.g. "freeze 3 waves, then fight at level 6 when their Flash is down".""",
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


HIGH_ELO = {"Diamond", "Master", "Grandmaster", "Challenger", "Ascendant", "Immortal", "Radiant"}


def depth(p):
    if p["rank"].split()[:1] and p["rank"].split()[0] in HIGH_ELO:
        return ("This is a high-elo player: their basics are solid, so don't coach basics unless they're clearly broken. "
                "Coach like a pro team's analyst: tempo, priority, wave states, jungle tracking, cooldown and summoner "
                "windows, objective setup, decision quality. A pro reviewer finds dozens of mistakes in a game at this "
                "level. Expect about 4-6 real mistakes or missed chances per 10 minutes of gameplay - small ones count (a wasted "
                "wave, a late recall, an unpunished cooldown) - and at most 2 clearly good decisions. Never pad.")
    return "Pitch advice at the level that will get them to the next rank."


def prompt(game, p, offset, prev_context, kits):
    g = GAMES[game]
    return f"""You are an elite {g['title']} coach reviewing a player's own POV recording.
Player: {g['who'](p)}. {depth(p)}
{g.get('layout', '')}
{g.get('rules', '')}
{kits_text(game, kits, p)}
Never invent ability names or effects - only use the kits above.
{feedback_text(game, p['agent'])}
{f"The player asked you to focus on: {p['focus']}" if p.get('focus') else ""}
This clip starts {int(offset // 60)} minutes into the full recording.
{f"What happened before this clip: {prev_context}" if prev_context else "This is the start of the recording (may include menus / loading)."}

Watch the whole clip and list concrete, timestamped coaching moments. For each:
- time: MM:SS relative to THIS clip, at the moment the decision was made (not when they died).
- category: one of {g['categories']}.
- situation: the facts on screen right before the decision (see above), written before you judge it.
{g['focus_hint'](p)}
Include good plays too (severity "good", category "good_play"), but only decisions that were right given the
situation - not plays that merely got a kill.
Only report what you can actually see; don't invent events."""


def partial_path(video):
    return video + ".coach.partial.json"


# "Time left" estimate: how long each step takes, learned from this PC's past reviews. compress/upload/watch are
# seconds per second of video (your CPU / your internet / Gemini), summary is plain seconds.
TIMING_FILE = CACHE_DIR / "timing.json"
STEPS = ("compress", "upload", "watch")
GUESS = {"compress": .05, "upload": .02, "watch": .3, "summary": 30}  # until measured: ~9 min per 25 min part


def load_speed():
    """(speeds, which of them were measured on this PC)"""
    try:
        saved = {k: float(v) for k, v in json.loads(TIMING_FILE.read_text(encoding="utf-8")).items() if k in GUESS}
    except (OSError, ValueError, TypeError, AttributeError):
        saved = {}
    return GUESS | saved, set(saved)


def save_speed(measured):
    """Blend new measurements into the saved ones (a first measurement replaces the guess outright)."""
    speed, known = load_speed()
    for k, v in measured.items():
        speed[k] = round(.5 * speed[k] + .5 * v if k in known else v, 6)
    CACHE_DIR.mkdir(exist_ok=True)
    TIMING_FILE.write_text(json.dumps({k: speed[k] for k in known | set(measured)}), encoding="utf-8")  # never save a guess


def analyze(path, api_key, models, p, progress):
    """models: chosen model first, then fallbacks for when its daily quota runs out.
    p: {game, agent, rank, focus, role?, opponent?} - agent is the champion for League.
    progress(msg, span=None, left=None): span = (start, end) share of the whole job (0..1) the step covers,
    left = estimated seconds until the report is done. progress may raise to cancel.
    Finished parts are saved next to the video, so a failed or cancelled review resumes where it stopped."""
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
    starts = list(range(0, int(total), g["segment"]))
    units = len(starts) + 0.15  # each part is 1 unit (mostly the watching), the summary is a small extra step
    span = lambda i, a, b: ((i + a) / units, (i + b) / units)

    # Same settings as a saved unfinished review of this video? Then keep its finished parts.
    key = {k: p.get(k, "") for k in ("game", "agent", "rank", "role", "opponent", "focus")} | {"segment": g["segment"]}
    try:
        saved = json.loads(Path(partial_path(path)).read_text(encoding="utf-8"))
        parts = saved["parts"] if saved["key"] == key else []
    except (OSError, ValueError, KeyError, TypeError):
        parts = []

    speed, _ = load_speed()
    lens = [min(g["segment"], total - st) for st in starts]

    def left(i, step="compress"):  # seconds until the report is done, from the start of `step` in part i
        rest = speed["summary"] + sum(speed[s] for s in STEPS) * sum(lens[i + 1:])
        return rest + sum(speed[s] for s in STEPS[STEPS.index(step):]) * lens[i] if i < len(starts) else speed["summary"]

    def timed(i, step):  # record a finished step's speed: later estimates in this run use it, and it's saved for next time
        speed[step] = (time.time() - t0) / lens[i]
        save_speed({step: speed[step]})
        return time.time()

    with tempfile.TemporaryDirectory() as tmp:
        for i, start in enumerate(starts):
            tag = f"Part {i + 1}/{len(starts)}"
            if i < len(parts):
                progress(f"{tag}: already reviewed, skipping...", span(i, 1, 1), left(i + 1))
                continue
            t0 = time.time()
            progress(f"{tag}: compressing video...", span(i, 0, .1), left(i, "compress"))
            seg = os.path.join(tmp, f"seg{i}.mp4")
            cut_segment(path, start, g["segment"], seg, g["frame"])
            t0 = timed(i, "compress")

            progress(f"{tag}: uploading VOD to coach...", span(i, .1, .25), left(i, "upload"))
            f = client.files.upload(file=seg)
            try:
                while f.state.name == "PROCESSING":
                    time.sleep(3)
                    f = client.files.get(name=f.name)
                if f.state.name != "ACTIVE":
                    raise CoachError(f"The coach couldn't read this video ({f.state.name}). Try re-saving it as MP4.")
                t0 = timed(i, "upload")

                progress(f"{tag}: coach is watching...", span(i, .25, 1), left(i, "watch"))
                resp = generate(client, progress, models,
                                contents=[f, prompt(game, p, start, parts[-1]["context"] if parts else "", kits)],
                                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                                   response_schema=SegmentReport,
                                                                   media_resolution=g["res"]))
            finally:
                try:
                    client.files.delete(name=f.name)
                except errors.APIError:
                    pass  # not fatal: Gemini auto-deletes uploads after 48h

            rep = resp.parsed or SegmentReport.model_validate_json(resp.text)
            parts.append({"map": rep.map, "context": rep.context,
                          "moments": [m.model_dump() | {"seconds": start + to_seconds(m.time)} for m in rep.moments]})
            Path(partial_path(path)).write_text(json.dumps({"key": key, "parts": parts}, indent=1), encoding="utf-8")
            timed(i, "watch")

    map_name = next((pt["map"] for pt in parts if pt["map"]), "")
    contexts = [pt["context"] for pt in parts]
    moments = [m for pt in parts for m in pt["moments"]]

    progress(f"Double-checking {len(moments)} notes...", span(len(starts), 0, .15), speed["summary"])
    t0 = time.time()
    notes = [{"i": i} | {k: m.get(k, "") for k in ("category", "situation", "what_happened", "severity", "title", "better")}
             for i, m in enumerate(moments)]
    summary = generate(
        client, progress, models,
        contents=f"""You coached a {g['title']} player ({g['who'](p)}) through a full match. {depth(p)}
Before they see your notes, check every one like a pro coach checking their own review:
- severity "good": first argue the case against the play. If it only worked because of the outcome (the enemy
  misplayed, a coin flip, a lucky dive), it was not good: give it the severity it deserves and say what the right play was.
  Keep "good" only if the decision was right given the situation.
- mistakes: keep them when the situation supports them; make better the exact alternative; set severity by what it cost.
{g.get('rules', '')}
Your notes (i = index): {json.dumps(notes)}
Match flow: {" | ".join(contexts)}
{kits_text(game, kits, p, everyone=False)}
Return a verdict for every note. Then write an overview, the 3 recurring mistakes that cost them the most
{g['summary_unit']}, ranked, each with a concrete drill or habit to fix it, and the strengths that hold up.""",
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Summary))
    s = summary.parsed or Summary.model_validate_json(summary.text)
    save_speed({"summary": time.time() - t0})
    verdicts = {v.i: v for v in s.verdicts}
    moments = [m | ({k: getattr(v, k) for k in ("category", "severity", "title", "better")} if (v := verdicts.get(i)) else {})
               for i, m in enumerate(moments)]  # re-grade only: letting it drop notes threw away real mistakes

    moments.sort(key=lambda m: m["seconds"])
    try:
        os.remove(partial_path(path))  # done: the full report replaces it
    except OSError:
        pass
    return {"video": os.path.basename(path), **{k: p.get(k, "") for k in ("game", "agent", "rank", "role", "opponent")},
            "map": map_name, "model": models[0] if models else "", **s.model_dump(exclude={"verdicts"}), "moments": moments}


if __name__ == "__main__":
    assert to_seconds("01:30") == 90 and to_seconds("1:02:03") == 3723 and to_seconds("0:05") == 5
    print("ok")
