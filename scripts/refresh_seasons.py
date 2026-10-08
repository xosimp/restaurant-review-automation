#!/usr/bin/env python3
"""Refresh the bundled season files (event_intel/seasons/*.json) from each
team's published schedule — run by hand on a developer's Mac, never by the
app or the scheduler (owner, 10/1/26: no new event APIs in the product; a
season file stays a file).

    python3 scripts/refresh_seasons.py            # dry run: what would change
    python3 scripts/refresh_seasons.py --write    # write the files
    python3 scripts/refresh_seasons.py --only nba-chicago-bulls

Per file it merges, never replaces:
  * a game already in the file keeps its external_id — matched by id, else
    by date (a day either side) and opponent;
  * its date, start, venue, status and result are updated from the
    source, and its TV only where the file has none (a final with its score is "completed"; postponed / cancelled
    say so; a time the source marks TBD stays empty);
  * a game the source adds (a playoff round, an NBA Cup date) is added;
  * nothing is deleted: a game the source no longer lists is reported for a
    person to check;
  * hand-kept facts stay: a neutral-site game's side, a holiday attribute,
    the file's own _about and sources.
Admin corrections live in catalog_events.overrides_json and are laid over
the file on load, so they survive a refresh too. Deploying the written files
loads them at boot (event_intel.store.load_bundled).
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEASONS = os.path.join(ROOT, "event_intel", "seasons")
CT = ZoneInfo("America/Chicago")
TIMEOUT = 30
UA = {"User-Agent": "Mozilla/5.0 (CavnarAI season refresh)"}

# slug -> where its schedule is published. ESPN paths are sport/league and
# the team's id there; the NHL and MLB are the leagues' own schedules.
SOURCES = {
    "nfl-chicago-bears": {"kind": "espn", "path": "football/nfl", "team": "chi", "types": (1, 2, 3)},
    "nba-chicago-bulls": {"kind": "espn", "path": "basketball/nba", "team": "chi", "types": (1, 2, 3)},
    "mls-chicago-fire": {"kind": "espn", "path": "soccer/usa.1", "team": "182", "soccer": True},
    "nhl-chicago-blackhawks": {"kind": "nhl", "team": "CHI"},
    "mlb-chicago-cubs": {"kind": "mlb", "team": 112},
    "mlb-chicago-white-sox": {"kind": "mlb", "team": 145},
}
SEASON_TYPES = {1: "preseason", 2: "regular", 3: "postseason"}


def _get(url):
    import requests
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _local(utc_iso):
    d = datetime.fromisoformat(str(utc_iso).replace("Z", "+00:00")).astimezone(CT)
    return d.strftime("%Y-%m-%d"), d.strftime("%H:%M")


def _norm(name):
    return " ".join(re.findall(r"[a-z0-9]+", str(name or "").lower()))


# ── sources → the file's event shape ──────────────────────────────────────

def from_espn(src, data, file_season):
    """ESPN team schedule events for every season type asked for."""
    out = []
    year = data["series"].get("_espn_season") or (file_season + 1 if src["path"].startswith(("basketball", "hockey"))
                                                   else file_season)
    base = f"https://site.api.espn.com/apis/site/v2/sports/{src['path']}/teams/{src['team']}/schedule?season={year}"
    # ESPN numbers soccer's season types its own way: results come from the
    # plain schedule, games still to play from fixture=true.
    queries = ([(base, None), (base + "&fixture=true", None)] if src.get("soccer")
               else [(base + f"&seasontype={st}", st) for st in src["types"]])
    for url, st in queries:
        try:
            body = _get(url)
        except Exception as e:
            print(f"  ! {url}: {e}")
            continue
        for e in body.get("events") or []:
            c = (e.get("competitions") or [{}])[0]
            teams = c.get("competitors") or []
            us = next((t for t in teams if str(t.get("team", {}).get("id")) == str(body.get("team", {}).get("id"))),
                      None)
            them = next((t for t in teams if t is not us), None)
            if not us or not them:
                continue
            day, k = _local(e["date"])
            st_name = (c.get("status") or {}).get("type", {}).get("name", "")
            status = ("completed" if (c.get("status") or {}).get("type", {}).get("completed")
                      else "postponed" if "POSTPONED" in st_name else "cancelled" if "CANCEL" in st_name
                      else "scheduled")
            res = None
            if status == "completed" and us.get("score") is not None and them.get("score") is not None:
                a = float(us["score"]["value"] if isinstance(us["score"], dict) else us["score"])
                b = float(them["score"]["value"] if isinstance(them["score"], dict) else them["score"])
                res = f"{'W' if a > b else 'L' if a < b else 'D'} {int(a)}-{int(b)}"
            tv = [b.get("media", {}).get("shortName").strip() for b in c.get("broadcasts") or []
                  if (b.get("media", {}).get("shortName") or "").strip()]
            if st is None:
                label = str((e.get("seasonType") or {}).get("name") or (e.get("season") or {}).get("name") or "").lower()
                stype = "postseason" if ("playoff" in label or "post" in label or "cup" in label) else "regular"
            else:
                stype = SEASON_TYPES.get(st, "regular")
            out.append({"external_id": str(e["id"]), "season_type": stype,
                        "date": day, "kickoff": k if c.get("timeValid", True) else None,
                        "home_away": us.get("homeAway"), "opponent": them["team"]["displayName"],
                        "venue": (c.get("venue") or {}).get("fullName"),
                        "broadcast": ", ".join(dict.fromkeys(tv)) or None, "status": status, "result": res})
    return out


def from_nhl(src, data, file_season):
    body = _get(f"https://api-web.nhle.com/v1/club-schedule-season/{src['team']}/{file_season}{file_season + 1}")
    out = []
    for g in body.get("games") or []:
        home = g["homeTeam"].get("abbrev") == src["team"] or \
            g["homeTeam"].get("commonName", {}).get("default") == data["series"].get("short_name")
        opp = g["awayTeam"] if home else g["homeTeam"]
        name = (opp.get("placeName", {}).get("default", "") + " " + opp.get("commonName", {}).get("default", "")).strip()
        day, k = _local(g["startTimeUTC"])
        state = g.get("gameState")
        sched = g.get("gameScheduleState")
        status = ("completed" if state in ("FINAL", "OFF") else "postponed" if sched == "PPD"
                  else "cancelled" if sched == "CNCL" else "scheduled")
        res = None
        if status == "completed":
            a = g["homeTeam" if home else "awayTeam"].get("score")
            b = g["awayTeam" if home else "homeTeam"].get("score")
            if a is not None and b is not None:
                res = f"{'W' if a > b else 'L'} {a}-{b}"
        tv = [t["network"].strip() for t in g.get("tvBroadcasts") or [] if t.get("countryCode") == "US"
              and (t.get("network") or "").strip()]
        out.append({"external_id": str(g["id"]),
                    "season_type": {1: "preseason", 2: "regular", 3: "postseason"}.get(g.get("gameType"), "regular"),
                    "date": day, "kickoff": k, "home_away": "home" if home else "away", "opponent": name,
                    "venue": (g.get("venue") or {}).get("default"), "broadcast": ", ".join(dict.fromkeys(tv)) or None,
                    "status": status, "result": res})
    return out


def from_mlb(src, data, file_season):
    body = _get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId={src['team']}&season={file_season}"
                f"&gameType=R,F,D,L,W")
    out = []
    for d in body.get("dates") or []:
        for g in d.get("games") or []:
            home = g["teams"]["home"]["team"]["id"] == src["team"]
            opp = g["teams"]["away" if home else "home"]["team"]["name"]
            state = (g.get("status") or {}).get("detailedState", "")
            status = ("completed" if state in ("Final", "Game Over", "Completed Early") else
                      "postponed" if "Postponed" in state else "cancelled" if "Cancelled" in state else "scheduled")
            tbd = bool((g.get("status") or {}).get("startTimeTBD"))
            res = None
            if status == "completed":
                a = g["teams"]["home" if home else "away"].get("score")
                b = g["teams"]["away" if home else "home"].get("score")
                if a is not None and b is not None:
                    res = f"{'W' if a > b else 'L'} {a}-{b}"
            gt = g.get("gameType")
            ev = {"external_id": str(g["gamePk"]),
                  "season_type": "preseason" if gt == "S" else "regular" if gt == "R" else "postseason",
                  "date": g.get("officialDate"), "kickoff": None if tbd else _local(g["gameDate"])[1],
                  "home_away": "home" if home else "away", "opponent": opp,
                  "venue": (g.get("venue") or {}).get("name"), "broadcast": None, "status": status, "result": res}
            if g.get("seriesDescription") and ev["season_type"] == "postseason":
                ev["week"] = g["seriesDescription"]
            if g.get("ifNecessary") == "Y":
                ev["attributes"] = {"if_necessary": True}
            elif g.get("ifNecessary") == "N":
                # MLB says outright when a game is happening (a series that
                # reached its Game 4): the merge clears a stale flag on it
                ev["attributes"] = {"if_necessary": False}
            out.append(ev)
    # MLB lists a postponed game twice under one gamePk (the postponement and
    # the game played later): the one that happened wins.
    rank = {"completed": 0, "scheduled": 1, "postponed": 2, "cancelled": 3}
    best = {}
    for ev in out:
        cur = best.get(ev["external_id"])
        if cur is None or rank.get(ev["status"], 9) < rank.get(cur["status"], 9):
            best[ev["external_id"]] = ev
    return list(best.values())


FETCH = {"espn": from_espn, "nhl": from_nhl, "mlb": from_mlb}


# ── the merge (no network; tests/test_refresh_seasons.py) ─────────────────

UPDATABLE = ("date", "kickoff", "venue", "broadcast", "status", "result", "season_type")


def merge(existing, fetched, today=None):
    """(events, changes): `existing` updated from `fetched` by the rules in
    the module docstring. `changes` is a list of human lines."""
    today = (today or date.today()).isoformat()
    events = [dict(e) for e in existing]
    by_id = {e["external_id"]: e for e in events}
    changes, seen = [], set()

    def _match(f):
        if f["external_id"] in by_id:
            return by_id[f["external_id"]]
        opp = _norm(f.get("opponent"))
        free = [e for e in events if e["external_id"] not in seen and _norm(e.get("opponent")) == opp]
        if f.get("date"):
            fd = date.fromisoformat(f["date"])
            near = [e for e in free if e.get("date") and abs((date.fromisoformat(e["date"]) - fd).days) <= 1
                    and (e.get("home_away") == f.get("home_away") or (e.get("attributes") or {}).get("neutral_site"))]
            if near:
                return min(near, key=lambda e: abs((date.fromisoformat(e["date"]) - fd).days))
        # A game with no date in the file yet (Week 18 TBD) takes the
        # source's date when the opponent and side match.
        return next((e for e in free if not e.get("date") and e.get("home_away") == f.get("home_away")), None)

    for f in fetched:
        e = _match(f)
        if e is None:
            new = {k: v for k, v in f.items() if v is not None or k in ("kickoff", "broadcast")}
            new.setdefault("week", None)
            if (new.get("attributes") or {}).get("if_necessary") is False:
                new["attributes"] = {k: v for k, v in new["attributes"].items() if k != "if_necessary"}
                if not new["attributes"]:
                    del new["attributes"]
            if new.get("status") != "completed":
                new.pop("result", None)
            events.append(new)
            by_id[new["external_id"]] = new
            seen.add(new["external_id"])
            changes.append(f"+ {f.get('date')} {f.get('home_away')} {f.get('opponent')} ({f.get('season_type')})")
            continue
        seen.add(e["external_id"])
        neutral = (e.get("attributes") or {}).get("neutral_site")
        for k in UPDATABLE:
            v = f.get(k)
            if k == "result" and not v:
                continue
            if k == "broadcast" and (not v or e.get("broadcast")):
                # TV only fills a gap: a source that lists one national
                # channel never replaces a fuller local listing (ESPN's
                # "NBA TV" for the file's "CHSN, NBA TV").
                continue
            if k == "kickoff" and v is None and e.get("kickoff") and f.get("date") == e.get("date"):
                continue                      # TBD at the source keeps a known time on the same day
            if k == "status" and e.get("status") == "completed" and v == "scheduled":
                continue                      # a played game never goes back
            if v != e.get(k):
                changes.append(f"~ {e.get('date')} {e.get('opponent')}: {k} {e.get(k)!r} -> {v!r}")
                e[k] = v
        if not neutral and f.get("home_away") and f["home_away"] != e.get("home_away"):
            changes.append(f"~ {e.get('date')} {e.get('opponent')}: home_away {e.get('home_away')!r} -> "
                           f"{f['home_away']!r}")
            e["home_away"] = f["home_away"]
        if (f.get("attributes") or {}).get("if_necessary") and not (e.get("attributes") or {}).get("if_necessary"):
            e.setdefault("attributes", {})["if_necessary"] = True
        elif ((f.get("attributes") or {}).get("if_necessary") is False and (e.get("attributes") or {}).get("if_necessary")
              and e.get("status") != "completed"):
            # the source says this game is happening: no longer "if necessary"
            changes.append(f"~ {e.get('date')} {e.get('opponent')}: no longer if necessary")
            del e["attributes"]["if_necessary"]
            if not e["attributes"]:
                del e["attributes"]
        if e.get("status") == "completed" and e.get("result") and (e.get("attributes") or {}).get("if_necessary"):
            changes.append(f"~ {e.get('date')} {e.get('opponent')}: played (if necessary)")
    for e in events:
        if e["external_id"] not in seen and fetched and (e.get("date") or "") >= today:
            changes.append(f"? {e.get('date')} {e.get('opponent')} ({e['external_id']}): not in the source any more — "
                           f"check it; nothing was deleted")
    events.sort(key=lambda x: (x.get("date") or "9999", x.get("kickoff") or ""))
    return events, changes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="write the merged files (default: dry run)")
    ap.add_argument("--only", help="one series slug")
    args = ap.parse_args(argv)
    total = 0
    for fn in sorted(os.listdir(SEASONS)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(SEASONS, fn)
        data = json.load(open(path, encoding="utf-8"))
        slug = data["series"]["slug"]
        if args.only and slug != args.only:
            continue
        src = SOURCES.get(slug)
        if not src:
            print(f"{fn}: no published source known — refresh it by hand")
            continue
        try:
            fetched = FETCH[src["kind"]](src, data, int(data.get("season")))
        except Exception as e:
            print(f"{fn}: could not read the source ({e}) — unchanged")
            continue
        events, changes = merge(data["events"], fetched)
        print(f"{fn}: {len(changes)} change(s)")
        for c in changes[:60]:
            print("  " + c)
        if len(changes) > 60:
            print(f"  … and {len(changes) - 60} more")
        total += len(changes)
        if args.write and changes:
            data["events"] = events
            data["fetched"] = date.today().isoformat()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
                fh.write("\n")
    if not args.write and total:
        print("\nDry run. Run again with --write to save, then commit and push; the deploy loads them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
