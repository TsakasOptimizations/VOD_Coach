# VOD Coach Privacy Policy

_Last updated: October 15, 2026_

VOD Coach is a free desktop app that reviews your VALORANT and League of Legends match recordings with Google Gemini.
The developer (TsakasTweaks) does not run any servers, does not have user accounts, and does not collect, receive or
sell any of your data. There are no ads and no analytics.

## What stays on your PC

These are stored only on your computer, in your user folder:

| What | Where |
|---|---|
| Your Google Gemini API key and app settings (game, agent/champion, rank, chosen coach) | `~/.vodcoach.json` |
| Your "Disagree" notes for the coach | `~/.vodcoach_feedback.json` |
| How many free reviews you used today | `~/.vodcoach_usage.json` |
| Downloaded agent, champion and rank data | `~/.vodcoach_cache/` |
| Error log | `~/.vodcoach.log` |
| Coaching reports | Next to each video, as `yourvideo.mp4.coach.json` |

Uninstalling the app does not delete these files. You can delete them yourself at any time.

## What is sent to third parties

**Google Gemini (only when you click "Analyze match").** Using your own API key, the app sends Google:
- your recording, compressed and split into parts (video **and audio**, so voice chat in the recording is included)
- the agent/champion, role, rank and focus text you entered
- your "Disagree" notes

Uploaded video parts are deleted from Google's servers right after each part is analyzed (Google also deletes
uploaded files automatically after 48 hours). How Google handles this content is governed by the
[Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms) and [Google's Privacy Policy](https://policies.google.com/privacy).
**Note:** on Gemini's free tier, Google states it may use submitted content to improve its products, and human
reviewers may read it. Don't analyze recordings that contain anything you wouldn't want Google to see.

**Game data services.** To show agent/champion abilities, icons and ranks, the app downloads public data from
[valorant-api.com](https://valorant-api.com), Riot Games' Data Dragon (ddragon.leagueoflegends.com) and
CommunityDragon (raw.communitydragon.org). Only standard web requests are made; no personal data is sent.

**Update check (GitHub download only).** The version downloaded from GitHub asks the GitHub API whether a newer
version exists. The Microsoft Store version does not do this; the Store handles updates.

**Google AI Studio.** The "Open AI Studio" button opens Google's website in your browser to create an API key; that
is covered by Google's own policies. The setup screen also loads Google's small logo from google.com.

Like any internet request, these services can see your IP address.

## Children

VOD Coach is not directed at children under 13 and does not knowingly collect information from anyone.

## Changes

If this policy changes, the updated version will be posted here with a new date.

## Contact

Questions or requests: open an issue at https://github.com/TsakasOptimizations/VOD_Coach/issues

VOD Coach isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially
involved in producing or managing Riot Games properties.
