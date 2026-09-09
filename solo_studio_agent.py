"""Solo Studio agent — core pipeline logic.

Pipeline: find local businesses with no website (Google Places) -> cold email
(Inkbox) -> on interested reply, deploy a WATERMARKED preview site (Claude
designs it, Netlify hosts it) -> on a second positive reply, send a Stripe
Checkout link -> poll Stripe until payment clears -> deploy the CLEAN final
site and email the live link.

Hard rules enforced here:
  * The clean (final) site is only ever deployed from stage 'paid', and a lead
    only reaches 'paid' when Stripe itself reports payment_status == "paid"
    for the exact Checkout Session we created for that lead.
  * Every stage transition is an atomic SQL "claim" (UPDATE ... WHERE stage=X)
    so duplicate replies, overlapping polls, or a double-clicked button cannot
    run the same step twice.
  * At most one Stripe Checkout Session is active per lead; a new one is only
    created if the previous one expired unpaid.
  * A lead that unsubscribes / declines is flagged do_not_contact and is never
    emailed again.

All configuration lives in config.json (written by the dashboard's Setup
page) — no environment variables, no editing code.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import requests

APP_NAME = "Solo Studio"

# --- self-update -----------------------------------------------------------
# Updated code is written to a folder in Application Support, never into the
# .app bundle: macOS guards app bundles, and editing one breaks its signature.
# The launcher prefers that folder when it holds a valid copy.
UPDATE_REPO = "yy68hpf7vf-ux/solo-studio"
UPDATE_BRANCH = "claude/solo-studio-pipeline-7r9508"
UPDATE_FILES = ("solo_studio_agent.py", "dashboard_app.py", "requirements.txt")
RESTART_EXIT_CODE = 42          # the launcher relaunches on this code


def code_fingerprint(directory: str | None = None) -> str:
    """Short hash of the app's own code, so the launcher can tell whether the
    copy already running is the same one it is about to start."""
    import hashlib
    d = directory or os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in ("solo_studio_agent.py", "dashboard_app.py"):
        try:
            with open(os.path.join(d, name), "rb") as f:
                h.update(f.read())
        except OSError:
            return ""
    return h.hexdigest()[:12]


def updates_dir() -> str:
    d = os.path.join(app_data_dir(), "app")
    os.makedirs(d, exist_ok=True)
    return d


def installed_version() -> dict:
    try:
        with open(os.path.join(updates_dir(), "installed.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def check_for_update(timeout: int = 15) -> dict:
    """Ask GitHub what the newest version is. Returns
    {ok, available, sha, short, message, date, installed}."""
    url = f"https://api.github.com/repos/{UPDATE_REPO}/commits/{UPDATE_BRANCH}"
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        return {"ok": False, "error": f"Couldn't reach GitHub: {e}"}
    if resp.status_code != 200:
        return {"ok": False,
                "error": f"GitHub said {resp.status_code}. Try again shortly."}
    data = resp.json()
    sha = data.get("sha", "")
    commit = data.get("commit", {})
    installed = installed_version().get("sha", "")
    return {
        "ok": True,
        "available": bool(sha) and sha != installed,
        "sha": sha,
        "short": sha[:7],
        "message": (commit.get("message") or "").splitlines()[0][:120],
        "date": (commit.get("committer") or {}).get("date", "")[:10],
        "installed": installed[:7],
        "never_updated": not installed,
    }


def apply_update(sha: str | None = None, timeout: int = 60) -> dict:
    """Download the newest code, check it actually parses, and install it.

    Nothing is overwritten until every file has downloaded and compiled, so a
    half-finished download can't leave a broken app behind.
    """
    if sha is None:
        info = check_for_update()
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error", "Update check failed.")}
        sha = info["sha"]
    fetched = {}
    for name in UPDATE_FILES:
        url = (f"https://raw.githubusercontent.com/{UPDATE_REPO}/{sha}/{name}")
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException as e:
            return {"ok": False, "error": f"Download of {name} failed: {e}"}
        if resp.status_code != 200:
            return {"ok": False,
                    "error": f"Couldn't download {name} (HTTP {resp.status_code})."}
        text = resp.text
        if name.endswith(".py"):
            # Guard against saving an error page or a truncated download.
            try:
                compile(text, name, "exec")
            except SyntaxError as e:
                return {"ok": False,
                        "error": f"The downloaded {name} isn't valid Python "
                                 f"({e}) — update cancelled, nothing changed."}
            if len(text) < 1000:
                return {"ok": False,
                        "error": f"The downloaded {name} looks truncated — "
                                 "update cancelled, nothing changed."}
        fetched[name] = text

    target = updates_dir()
    try:
        for name, text in fetched.items():
            tmp = os.path.join(target, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, os.path.join(target, name))
        with open(os.path.join(target, "installed.json"), "w", encoding="utf-8") as f:
            json.dump({"sha": sha, "applied_at": _now()}, f)
    except OSError as e:
        return {"ok": False, "error": f"Couldn't save the update: {e}"}
    return {"ok": True, "sha": sha, "short": sha[:7]}


# A line a day, for the top of the dashboard.
#
# Most of these are written for this particular job — one person, cold email,
# a lot of silence between the yeses — because a line about *this* is worth
# more on a Tuesday morning than a famous one about something else. The few
# that are quoted are proverbs or are attributed to a source I can point at;
# I would rather print no name than the wrong one.
DAILY_LINES = [
    ("The tenth no is closer to a yes than the first one was.", ""),
    ("A business with no website is not a hard sell. It's an obvious one.", ""),
    ("Send the email you'd want to receive.", ""),
    ("Fall seven times, stand up eight.", "Japanese proverb"),
    ("Amateurs sit and wait for inspiration, the rest of us just get up and "
     "go to work.", "Stephen King, On Writing"),
    ("Twenty good emails beat two hundred lazy ones.", ""),
    ("The quiet weeks are the ones that decide it.", ""),
    ("You are not interrupting them. You are offering to fix something "
     "they already know is broken.", ""),
    ("Every studio you admire started with one client who said yes.", ""),
    ("Slow is smooth. Smooth is fast.", ""),
    ("A no today is a maybe next spring. Keep the list.", ""),
    ("Design the site as if they've already paid for it.", ""),
    ("The work you do before anyone is watching is the work.", ""),
    ("Nobody was ever talked into caring. They were shown.", ""),
    ("Ship it slightly before you feel ready.", ""),
    ("If you can't write the email in five sentences, you don't know the "
     "offer well enough yet.", ""),
    ("Being early is a kind of advantage nobody can copy.", ""),
    ("Small and finished beats big and pending.", ""),
    ("The person who replies at 11pm is the person who wants it.", ""),
    ("Charge for the outcome, not the hours.", ""),
    ("A well-run day looks boring from the outside.", ""),
    ("Do the follow-up. That's where the money is.", ""),
    ("Your first ten clients teach you what to sell to the next hundred.", ""),
    ("Make it easy to say yes and impossible to misunderstand.", ""),
    ("Consistency is a skill, not a personality trait.", ""),
    ("The gap between the work you make and the work you want to make "
     "closes by making more work.", ""),
    ("Don't polish the pitch. Polish the thing you're pitching.", ""),
    ("Answer fast. Speed reads as competence.", ""),
    ("The best time to plant a tree was twenty years ago. "
     "The second best time is now.", "Proverb"),
    ("You only need this to work once to know it works.", ""),
    ("Rejection is information, not a verdict.", ""),
    ("Build the boring machine that runs while you sleep.", ""),
    ("One good local business tells three others.", ""),
    ("A deadline you set for yourself still counts.", ""),
    ("Some days the job is just: send the next one.", ""),
    ("Price it so you'd be glad to do it again.", ""),
    ("You can't control the reply. You can control the sending.", ""),
    ("Make something a stranger would pay for. Then find the stranger.", ""),
    ("Finish something today, even if it's small.", ""),
    ("The compounding is invisible right up until it isn't.", ""),
]


def line_for_today(today: date | None = None) -> dict:
    """The same line all day, a different one tomorrow, cycling the whole list
    before any of it comes round again."""
    day = today or date.today()
    text, source = DAILY_LINES[day.toordinal() % len(DAILY_LINES)]
    return {"text": text, "source": source}


# Trades where a lot of businesses still have no website. Restaurants, salons
# and gyms are deliberately absent — nearly all of them have one, so searching
# for them burns Google calls to find nobody.
# Google Text Search: 5,000 calls a month free, then roughly $32 per thousand.
# The cap sits under the free line on purpose.
GOOGLE_FREE_CALLS_MONTH = 5000
GOOGLE_CALL_CAP = 4500


# Looking an address up with a model and web search is the priciest thing the
# app does per lead: $10 per 1,000 searches on top of tokens. Haiku instead of
# Opus is a fifth the token price, and this is an extraction job, not a
# reasoning one.
RESEARCH_MODEL = "claude-haiku-4-5"
MAIN_MODEL = "claude-opus-5"

# One dial for what the app is allowed to spend on Claude. Finding leads never
# touches it — that is Google and OpenStreetMap — so this is about looking up
# addresses, reading replies, and answering you.
#
# Designing a site is not on the dial at any level: it only runs when somebody
# has asked for one, it is the thing being sold, and it should be the best the
# account can do.
SPEND_LEVELS = {
    "off":    {"lookups_month": 0,   "lookups_day": 0,  "small_model": True,
               "label": "Off — no Claude credit spent on lookups at all"},
    "frugal": {"lookups_month": 50,  "lookups_day": 10, "small_model": True,
               "label": "Frugal — cheap model, a few paid lookups a day"},
    "normal": {"lookups_month": 200, "lookups_day": 25, "small_model": False,
               "label": "Normal — best model for replies, more lookups"},
}
DEFAULT_SPEND = "frugal"
# Two web searches at $10/1,000 plus a small model's tokens on what comes back.
LOOKUP_DOLLARS = 0.035


def spend_plan(cfg: dict) -> dict:
    """What this app may spend, and on which model."""
    plan = dict(SPEND_LEVELS.get(cfg.get("spend_level") or DEFAULT_SPEND,
                                 SPEND_LEVELS[DEFAULT_SPEND]))
    # An explicit cap still wins — the dial sets it, it doesn't lock it.
    if cfg.get("monthly_lookup_cap") not in (None, ""):
        plan["lookups_month"] = max(0, setting_int(cfg, "monthly_lookup_cap",
                                                   plan["lookups_month"]))
    return plan


def thinking_model(cfg: dict) -> str:
    """The model for work that is judgement, not extraction: reading a reply,
    answering a question. Small when the dial says to be frugal."""
    if spend_plan(cfg)["small_model"]:
        return (cfg.get("research_model") or "").strip() or RESEARCH_MODEL
    return (cfg.get("anthropic_model") or "").strip() or MAIN_MODEL
LOOKUP_CAP = 200          # paid lookups a month, before it stops spending


def _web_search_tool_for(model: str) -> str:
    """The web-search tool variant a model actually accepts.

    The newer one needs Opus 4.6+ or Sonnet 4.6+; Haiku takes the basic one,
    and sending the wrong variant is a 400 rather than a graceful fallback.
    """
    modern = ("claude-opus-", "claude-sonnet-5", "claude-sonnet-4-6",
              "claude-fable-", "claude-mythos-")
    return ("web_search_20260209" if model.startswith(modern)
            else "web_search_20250305")


def setting_int(cfg: dict, key: str, default: int) -> int:
    """A whole-number setting, where zero means zero.

    The obvious `cfg.get(k, d) or d` turns a deliberate 0 into the default,
    which for a spending cap is the opposite of what was asked for.
    """
    value = cfg.get(key, default)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lookup_meter_key() -> str:
    return "paid_lookups_" + datetime.now(timezone.utc).strftime("%Y-%m")


def _lookup_day_key() -> str:
    return "paid_lookups_" + datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _google_meter_key() -> str:
    return "google_calls_" + datetime.now(timezone.utc).strftime("%Y-%m")


# Every city JARVIS works through, largest first within each state. Names
# only: a name is something to be sure of, whereas coordinates typed from
# memory are a wrong search that costs money. Each one is looked up on the map
# once, when the crawl reaches it, and remembered forever after.
US_CITIES_BY_STATE = {
    "AL": "Birmingham, Montgomery, Huntsville, Mobile, Tuscaloosa, Hoover, Dothan, Auburn, Decatur, Madison",
    "AK": "Anchorage, Fairbanks, Juneau, Wasilla, Sitka, Ketchikan, Kenai, Palmer",
    "AZ": "Phoenix, Tucson, Mesa, Chandler, Scottsdale, Glendale, Gilbert, Tempe, Peoria, Surprise, Yuma, Flagstaff, Goodyear, Casa Grande",
    "AR": "Little Rock, Fayetteville, Fort Smith, Springdale, Jonesboro, Rogers, Conway, North Little Rock, Bentonville, Pine Bluff",
    "CA": "Los Angeles, San Diego, San Jose, San Francisco, Fresno, Sacramento, Long Beach, Oakland, Bakersfield, Anaheim, Santa Ana, Riverside, Stockton, Irvine, Chula Vista, Fremont, Modesto, Fontana, Oxnard, Moreno Valley, Huntington Beach, Glendale, Santa Clarita, Garden Grove, Oceanside, Rancho Cucamonga, Ontario, Elk Grove, Corona, Lancaster, Palmdale, Salinas, Hayward, Pomona, Escondido, Sunnyvale, Torrance, Pasadena, Orange, Fullerton, Visalia, Roseville, Concord, Victorville, Santa Rosa, Vallejo, Berkeley, El Monte, Downey, Costa Mesa, Inglewood, Carlsbad, Fairfield, Ventura, Temecula, Antioch, Richmond, West Covina, Murrieta, Norwalk, Daly City, Burbank, Santa Maria, El Cajon, Rialto, San Mateo, Compton, Clovis, Jurupa Valley, Vista, South Gate, Mission Viejo, Vacaville, Carson, Hesperia, Redding, Santa Monica, Westminster, Santa Barbara, Chico, Whittier, Newport Beach, San Leandro, Hawthorne, Citrus Heights, Alhambra, Tracy, Livermore, Buena Park, Lakewood, Merced, Hemet, Chino, Menifee, Lake Forest, Napa, Redwood City, Bellflower, Indio, Tustin, Baldwin Park, Chino Hills, Mountain View, Alameda, Upland, San Ramon, Folsom, Pleasanton, Union City, Perris, Manteca, Lynwood, Apple Valley, Redlands, Turlock, Milpitas, Redondo Beach, Rancho Cordova, Yorba Linda, Palo Alto, Davis, Camarillo, Walnut Creek, Pittsburg, South San Francisco, Yuba City, San Clemente, Laguna Niguel, Pico Rivera, Montebello, Lodi, Madera, Santa Cruz, La Habra, Encinitas, Monterey Park, Tulare, Cupertino, Gardena, National City, Rocklin, Petaluma, Huntington Park, San Rafael, La Mesa, Arcadia, Fountain Valley, Diamond Bar, Woodland, Santee, Lake Elsinore, Porterville, Paramount, Eastvale, Rosemead, Hanford, Highland, Brentwood, Novato, Colton, Cathedral City, Delano, Yucaipa, Watsonville, Placentia, Glendora, Gilroy, Palm Desert, Cerritos, West Sacramento, Aliso Viejo, Poway, La Mirada, Rancho Santa Margarita, Cypress, Dublin, Covina, Azusa, Palm Springs, San Luis Obispo, Ceres, San Jacinto, Lincoln, Newark, Lompoc, El Centro, Danville, Bell Gardens, Coachella, Rancho Palos Verdes, San Bruno, Rohnert Park, Brea, La Puente, Campbell, San Gabriel, Beaumont, Los Banos, Adelanto, Culver City, Calexico, Stanton, La Quinta, Monrovia, Martinez, Hollister",
    "CO": "Denver, Colorado Springs, Aurora, Fort Collins, Lakewood, Thornton, Arvada, Westminster, Pueblo, Centennial, Boulder, Greeley, Longmont, Loveland, Broomfield, Grand Junction, Castle Rock, Commerce City, Parker, Littleton",
    "CT": "Bridgeport, New Haven, Hartford, Stamford, Waterbury, Norwalk, Danbury, New Britain, Bristol, Meriden, Milford, West Haven, Middletown, Norwich, Shelton, Torrington",
    "DE": "Wilmington, Dover, Newark, Middletown, Smyrna, Milford, Seaford, Georgetown",
    "FL": "Jacksonville, Miami, Tampa, Orlando, St. Petersburg, Hialeah, Port St. Lucie, Cape Coral, Tallahassee, Fort Lauderdale, Pembroke Pines, Hollywood, Gainesville, Miramar, Coral Springs, Palm Bay, West Palm Beach, Clearwater, Lakeland, Pompano Beach, Miami Gardens, Davie, Boca Raton, Sunrise, Deltona, Plantation, Palm Coast, Fort Myers, Largo, Melbourne, Deerfield Beach, Boynton Beach, Lauderhill, Weston, Kissimmee, Homestead, Delray Beach, Daytona Beach, Tamarac, North Miami, Wellington, Jupiter, Ocala, Port Orange, Margate, Coconut Creek, Sanford, Sarasota, Pensacola, Bradenton, Palm Beach Gardens, Pinellas Park, Coral Gables, Doral, Bonita Springs, Apopka, Titusville, North Port, Fort Pierce, Winter Haven, Altamonte Springs, Cutler Bay, North Lauderdale, Oakland Park, Greenacres, Ormond Beach, Clermont, New Smyrna Beach, Lake Worth, Winter Garden, Casselberry",
    "GA": "Atlanta, Augusta, Columbus, Macon, Savannah, Athens, Sandy Springs, Roswell, Johns Creek, Albany, Warner Robins, Alpharetta, Marietta, Valdosta, Smyrna, Dunwoody, Rome, East Point, Milton, Gainesville, Peachtree Corners, Newnan, Douglasville, Kennesaw, Lawrenceville, Statesboro, Duluth, Stockbridge, Woodstock, Carrollton",
    "HI": "Honolulu, Hilo, Kailua, Kapolei, Kaneohe, Waipahu, Pearl City, Mililani, Kahului, Ewa Beach",
    "ID": "Boise, Meridian, Nampa, Idaho Falls, Pocatello, Caldwell, Coeur d'Alene, Twin Falls, Post Falls, Lewiston",
    "IL": "Chicago, Aurora, Joliet, Naperville, Rockford, Springfield, Elgin, Peoria, Champaign, Waukegan, Cicero, Bloomington, Arlington Heights, Evanston, Schaumburg, Bolingbrook, Palatine, Skokie, Des Plaines, Orland Park, Tinley Park, Oak Lawn, Berwyn, Mount Prospect, Normal, Wheaton, Hoffman Estates, Oak Park, Downers Grove, Elmhurst, Glenview, DeKalb, Lombard, Belleville, Moline, Buffalo Grove, Bartlett, Urbana, Quincy, Crystal Lake",
    "IN": "Indianapolis, Fort Wayne, Evansville, South Bend, Carmel, Fishers, Bloomington, Hammond, Gary, Lafayette, Muncie, Terre Haute, Kokomo, Noblesville, Anderson, Greenwood, Elkhart, Mishawaka, Lawrence, Jeffersonville, Columbus, Portage, New Albany, Richmond, Valparaiso, Goshen, Michigan City, Westfield",
    "IA": "Des Moines, Cedar Rapids, Davenport, Sioux City, Iowa City, Waterloo, Council Bluffs, Ames, West Des Moines, Dubuque, Ankeny, Urbandale, Cedar Falls, Marion, Bettendorf, Mason City, Clinton, Burlington",
    "KS": "Wichita, Overland Park, Kansas City, Olathe, Topeka, Lawrence, Shawnee, Manhattan, Lenexa, Salina, Hutchinson, Leavenworth, Leawood, Dodge City, Garden City, Emporia",
    "KY": "Louisville, Lexington, Bowling Green, Owensboro, Covington, Richmond, Georgetown, Florence, Hopkinsville, Nicholasville, Elizabethtown, Henderson, Frankfort, Jeffersontown, Paducah",
    "LA": "New Orleans, Baton Rouge, Shreveport, Lafayette, Lake Charles, Kenner, Bossier City, Monroe, Alexandria, Houma, Marrero, New Iberia, Slidell, Central, Ruston",
    "ME": "Portland, Lewiston, Bangor, South Portland, Auburn, Biddeford, Sanford, Saco, Augusta, Westbrook",
    "MD": "Baltimore, Columbia, Germantown, Silver Spring, Waldorf, Glen Burnie, Ellicott City, Frederick, Dundalk, Rockville, Bethesda, Gaithersburg, Towson, Bowie, Aspen Hill, Wheaton, Bel Air, Potomac, Severn, Hagerstown, Annapolis, Odenton, Catonsville, Salisbury",
    "MA": "Boston, Worcester, Springfield, Cambridge, Lowell, Brockton, New Bedford, Quincy, Lynn, Fall River, Newton, Somerville, Lawrence, Framingham, Haverhill, Waltham, Malden, Brookline, Medford, Taunton, Chicopee, Weymouth, Revere, Peabody, Methuen, Barnstable, Pittsfield, Attleboro, Everett, Salem, Westfield, Leominster, Fitchburg, Beverly, Holyoke, Marlborough, Woburn, Chelsea",
    "MI": "Detroit, Grand Rapids, Warren, Sterling Heights, Ann Arbor, Lansing, Flint, Dearborn, Livonia, Troy, Westland, Farmington Hills, Kalamazoo, Wyoming, Southfield, Rochester Hills, Taylor, Saint Clair Shores, Pontiac, Dearborn Heights, Royal Oak, Novi, Battle Creek, Saginaw, Kentwood, East Lansing, Roseville, Portage, Midland, Muskegon, Lincoln Park, Bay City, Jackson, Holland, Port Huron",
    "MN": "Minneapolis, Saint Paul, Rochester, Duluth, Bloomington, Brooklyn Park, Plymouth, Woodbury, Maple Grove, Saint Cloud, Eagan, Eden Prairie, Coon Rapids, Blaine, Burnsville, Lakeville, Minnetonka, Apple Valley, Edina, Saint Louis Park, Mankato, Moorhead, Shakopee, Maplewood, Cottage Grove, Richfield",
    "MS": "Jackson, Gulfport, Southaven, Hattiesburg, Biloxi, Meridian, Tupelo, Olive Branch, Greenville, Horn Lake, Pearl, Madison, Starkville, Clinton, Columbus",
    "MO": "Kansas City, Saint Louis, Springfield, Columbia, Independence, Lee's Summit, O'Fallon, Saint Joseph, Saint Charles, Blue Springs, Saint Peters, Florissant, Joplin, Chesterfield, Jefferson City, Cape Girardeau, Wildwood, University City, Ballwin, Raytown, Liberty",
    "MT": "Billings, Missoula, Great Falls, Bozeman, Butte, Helena, Kalispell, Havre, Anaconda, Belgrade",
    "NE": "Omaha, Lincoln, Bellevue, Grand Island, Kearney, Fremont, Hastings, Norfolk, North Platte, Papillion",
    "NV": "Las Vegas, Henderson, Reno, North Las Vegas, Sparks, Carson City, Fernley, Elko, Mesquite, Boulder City",
    "NH": "Manchester, Nashua, Concord, Derry, Dover, Rochester, Salem, Merrimack, Londonderry, Hudson, Keene, Portsmouth",
    "NJ": "Newark, Jersey City, Paterson, Elizabeth, Edison, Woodbridge, Lakewood, Toms River, Hamilton, Trenton, Clifton, Camden, Brick, Cherry Hill, Passaic, Middletown, Union City, Old Bridge, Gloucester, East Orange, Bayonne, Franklin, North Bergen, Vineland, Union, Piscataway, New Brunswick, Jackson, Wayne, Irvington, Parsippany, Howell, Perth Amboy, Hoboken, Plainfield, West New York, Washington, East Brunswick, Bloomfield, West Orange",
    "NM": "Albuquerque, Las Cruces, Rio Rancho, Santa Fe, Roswell, Farmington, Clovis, Hobbs, Alamogordo, Carlsbad, Gallup, Los Lunas, Deming",
    "NY": "New York, Buffalo, Yonkers, Rochester, Syracuse, Albany, New Rochelle, Mount Vernon, Schenectady, Utica, White Plains, Hempstead, Troy, Niagara Falls, Binghamton, Freeport, Valley Stream, Long Beach, Rome, North Tonawanda, Ithaca, Poughkeepsie, Jamestown, Elmira, Newburgh, Middletown, Auburn, Watertown, Glen Cove, Saratoga Springs, Kingston, Peekskill, Lockport, Plattsburgh, Cortland, Oswego, Beacon, Batavia, Ellenville, Monticello, Liberty, Newark, Geneva, Canandaigua, Oneonta, Amsterdam, Gloversville, Johnstown, Hudson, Catskill",
    "NC": "Charlotte, Raleigh, Greensboro, Durham, Winston-Salem, Fayetteville, Cary, Wilmington, High Point, Concord, Asheville, Greenville, Gastonia, Jacksonville, Chapel Hill, Rocky Mount, Huntersville, Burlington, Wilson, Kannapolis, Apex, Hickory, Wake Forest, Indian Trail, Mooresville, Goldsboro, Monroe, Salisbury, Matthews, New Bern, Sanford, Cornelius, Garner, Thomasville, Statesville, Asheboro, Mint Hill, Kernersville, Morrisville, Lumberton",
    "ND": "Fargo, Bismarck, Grand Forks, Minot, West Fargo, Williston, Dickinson, Mandan, Jamestown, Wahpeton",
    "OH": "Columbus, Cleveland, Cincinnati, Toledo, Akron, Dayton, Parma, Canton, Youngstown, Lorain, Hamilton, Springfield, Kettering, Elyria, Lakewood, Cuyahoga Falls, Middletown, Euclid, Newark, Mansfield, Mentor, Beavercreek, Cleveland Heights, Strongsville, Dublin, Fairfield, Findlay, Warren, Lancaster, Lima, Huber Heights, Westerville, Marion, Grove City, Reynoldsburg, Delaware, Brunswick, Stow, Upper Arlington, Gahanna",
    "OK": "Oklahoma City, Tulsa, Norman, Broken Arrow, Lawton, Edmond, Moore, Midwest City, Enid, Stillwater, Muskogee, Bartlesville, Owasso, Shawnee, Ponca City, Ardmore, Duncan, Yukon, Del City, Bixby",
    "OR": "Portland, Salem, Eugene, Gresham, Hillsboro, Beaverton, Bend, Medford, Springfield, Corvallis, Albany, Tigard, Lake Oswego, Keizer, Grants Pass, Oregon City, McMinnville, Redmond, Tualatin, West Linn, Woodburn, Newberg, Forest Grove, Roseburg, Klamath Falls, Ashland",
    "PA": "Philadelphia, Pittsburgh, Allentown, Erie, Reading, Scranton, Bethlehem, Lancaster, Harrisburg, York, Altoona, Wilkes-Barre, Chester, Williamsport, Easton, Lebanon, Hazleton, New Castle, Johnstown, Norristown, McKeesport, Chambersburg, Carlisle, Hanover, Pottstown, Sharon, Bloomsburg, West Chester, Butler, Washington, Meadville, Indiana, Greensburg, Uniontown, Oil City, Warren, Bradford, Sunbury, Lock Haven, Pottsville",
    "RI": "Providence, Warwick, Cranston, Pawtucket, East Providence, Woonsocket, Newport, Central Falls, Westerly, Bristol",
    "SC": "Charleston, Columbia, North Charleston, Mount Pleasant, Rock Hill, Greenville, Summerville, Sumter, Goose Creek, Hilton Head Island, Florence, Spartanburg, Myrtle Beach, Aiken, Anderson, Greer, Mauldin, Greenwood, North Augusta, Easley",
    "SD": "Sioux Falls, Rapid City, Aberdeen, Brookings, Watertown, Mitchell, Yankton, Pierre, Huron, Vermillion",
    "TN": "Nashville, Memphis, Knoxville, Chattanooga, Clarksville, Murfreesboro, Franklin, Jackson, Johnson City, Bartlett, Hendersonville, Kingsport, Collierville, Smyrna, Cleveland, Brentwood, Germantown, Columbia, La Vergne, Gallatin, Cookeville, Mount Juliet, Lebanon, Morristown, Oak Ridge, Maryville, Bristol, Farragut",
    "TX": "Houston, San Antonio, Dallas, Austin, Fort Worth, El Paso, Arlington, Corpus Christi, Plano, Laredo, Lubbock, Garland, Irving, Amarillo, Grand Prairie, Brownsville, McKinney, Frisco, Pasadena, Killeen, McAllen, Mesquite, Midland, Denton, Waco, Carrollton, Round Rock, Abilene, Pearland, Richardson, Odessa, Sugar Land, College Station, Beaumont, Lewisville, Tyler, League City, San Angelo, Allen, Wichita Falls, Longview, Edinburg, Mission, Bryan, Baytown, Pharr, Temple, Missouri City, Flower Mound, Harlingen, North Richland Hills, Victoria, Conroe, New Braunfels, Mansfield, Cedar Park, Rowlett, Port Arthur, Euless, Georgetown, Pflugerville, DeSoto, San Marcos, Grapevine, Bedford, Galveston, Cedar Hill, Texas City, Wylie, Haltom City, Keller, Coppell, Rockwall, Huntsville, Duncanville, Sherman, The Colony, Burleson, Hurst, Lancaster, Texarkana, Friendswood, Weslaco, Socorro, Horizon City, Canutillo, San Elizario, Anthony, Fabens, Clint, Vinton",
    "UT": "Salt Lake City, West Valley City, Provo, West Jordan, Orem, Sandy, Ogden, St. George, Layton, South Jordan, Lehi, Millcreek, Taylorsville, Logan, Murray, Draper, Bountiful, Riverton, Herriman, Spanish Fork, Roy, Pleasant Grove, Kearns, Tooele, Cottonwood Heights, Springville",
    "VT": "Burlington, South Burlington, Rutland, Barre, Montpelier, Winooski, St. Albans, Newport, Vergennes, Middlebury",
    "VA": "Virginia Beach, Chesapeake, Norfolk, Arlington, Richmond, Newport News, Alexandria, Hampton, Roanoke, Portsmouth, Suffolk, Lynchburg, Harrisonburg, Charlottesville, Danville, Manassas, Petersburg, Fredericksburg, Winchester, Salem, Staunton, Herndon, Hopewell, Fairfax, Waynesboro, Blacksburg, Christiansburg, Radford, Bristol, Martinsville",
    "WA": "Seattle, Spokane, Tacoma, Vancouver, Bellevue, Kent, Everett, Renton, Federal Way, Spokane Valley, Yakima, Kirkland, Bellingham, Kennewick, Auburn, Pasco, Marysville, Lakewood, Redmond, Shoreline, Richland, Sammamish, Burien, Olympia, Lacey, Edmonds, Puyallup, Bremerton, Longview, Wenatchee, Mount Vernon, Walla Walla, Pullman, Des Moines, SeaTac, Bothell, Issaquah, Mercer Island",
    "WV": "Charleston, Huntington, Morgantown, Parkersburg, Wheeling, Martinsburg, Fairmont, Beckley, Clarksburg, Weirton, Bluefield, Hurricane",
    "WI": "Milwaukee, Madison, Green Bay, Kenosha, Racine, Appleton, Waukesha, Eau Claire, Oshkosh, Janesville, West Allis, La Crosse, Sheboygan, Wauwatosa, Fond du Lac, New Berlin, Wausau, Brookfield, Beloit, Greenfield, Manitowoc, Sun Prairie, Superior, Stevens Point, Neenah, Fitchburg, Mount Pleasant",
    "WY": "Cheyenne, Casper, Laramie, Gillette, Rock Springs, Sheridan, Green River, Evanston, Riverton, Cody",
    "DC": "Washington",
}


# Where those cities are, from public-domain US city/coordinate data. Built in
# for two reasons: the map can show the whole route at once instead of filling
# in a dot at a time, and the crawl doesn't spend a Google lookup per city just
# to learn where it is. The handful not in here still get looked up when the
# crawl reaches them.
CITY_COORDS = json.loads(r"""{"Anchorage, AK":[61.2116,-149.8761],"Fairbanks, AK":[64.8402,-147.7104],"Juneau, AK":[58.3628,-134.5294],"Wasilla, AK":[61.5814,-149.4394],"Sitka, AK":[57.0514,-135.3166],"Ketchikan, AK":[55.372,-131.6832],"Kenai, AK":[60.6145,-151.2546],"Palmer, AK":[61.6138,-149.0653],"Birmingham, AL":[33.519,-86.8014],"Montgomery, AL":[32.3743,-86.3118],"Huntsville, AL":[34.7269,-86.5673],"Mobile, AL":[30.6959,-88.0434],"Tuscaloosa, AL":[33.1969,-87.5627],"Hoover, AL":[33.4054,-86.8114],"Dothan, AL":[31.2029,-85.418],"Auburn, AL":[32.602,-85.489],"Decatur, AL":[34.5896,-86.9887],"Madison, AL":[34.6578,-86.8056],"Little Rock, AR":[34.7483,-92.2819],"Fayetteville, AR":[36.052,-94.1534],"Fort Smith, AR":[35.3653,-94.411],"Springdale, AR":[36.1835,-94.1762],"Jonesboro, AR":[35.833,-90.6965],"Rogers, AR":[36.3363,-94.1148],"Conway, AR":[35.0842,-92.4236],"North Little Rock, AR":[34.767,-92.2654],"Bentonville, AR":[36.3577,-94.2224],"Pine Bluff, AR":[34.2154,-91.9958],"Phoenix, AZ":[33.451,-112.0685],"Tucson, AZ":[32.2139,-110.9694],"Mesa, AZ":[33.4317,-111.8469],"Chandler, AZ":[33.3301,-111.8632],"Scottsdale, AZ":[33.5218,-111.9049],"Glendale, AZ":[33.5311,-112.1767],"Gilbert, AZ":[33.35,-111.8092],"Tempe, AZ":[33.4273,-111.9307],"Peoria, AZ":[33.5761,-112.2344],"Surprise, AZ":[33.63,-112.3314],"Yuma, AZ":[32.7015,-114.6424],"Flagstaff, AZ":[35.1859,-111.662],"Goodyear, AZ":[33.4368,-112.3834],"Casa Grande, AZ":[32.8927,-111.7561],"Los Angeles, CA":[33.9731,-118.2479],"San Diego, CA":[32.7185,-117.1593],"San Jose, CA":[37.3894,-121.8868],"San Francisco, CA":[37.775,-122.4183],"Fresno, CA":[36.8411,-119.8004],"Sacramento, CA":[38.5816,-121.4933],"Long Beach, CA":[33.7705,-118.1885],"Oakland, CA":[37.7806,-122.2166],"Bakersfield, CA":[35.3866,-119.0171],"Anaheim, CA":[33.8427,-117.954],"Santa Ana, CA":[33.7502,-117.8577],"Riverside, CA":[33.9924,-117.3694],"Stockton, CA":[37.958,-121.2876],"Irvine, CA":[33.7357,-117.7672],"Chula Vista, CA":[32.64,-117.0833],"Fremont, CA":[37.5605,-121.9999],"Modesto, CA":[37.6746,-121.0113],"Fontana, CA":[34.0589,-117.4383],"Oxnard, CA":[34.2141,-119.175],"Moreno Valley, CA":[33.8858,-117.2211],"Huntington Beach, CA":[33.7152,-118.0088],"Glendale, CA":[34.1716,-118.2899],"Santa Clarita, CA":[34.4597,-118.489],"Garden Grove, CA":[33.7857,-117.9318],"Oceanside, CA":[33.1951,-117.3776],"Rancho Cucamonga, CA":[34.1339,-117.5991],"Ontario, CA":[34.0631,-117.6197],"Elk Grove, CA":[38.4127,-121.3599],"Corona, CA":[33.8815,-117.6078],"Lancaster, CA":[34.6909,-118.1491],"Palmdale, CA":[34.5715,-118.0613],"Salinas, CA":[36.6677,-121.6596],"Hayward, CA":[37.6564,-122.0957],"Pomona, CA":[34.0433,-117.7521],"Escondido, CA":[33.1101,-117.07],"Sunnyvale, CA":[37.3689,-122.0353],"Torrance, CA":[33.8268,-118.3118],"Pasadena, CA":[34.1468,-118.1391],"Orange, CA":[33.7877,-117.8755],"Fullerton, CA":[33.8796,-117.8951],"Visalia, CA":[36.3114,-119.3065],"Roseville, CA":[38.7346,-121.234],"Concord, CA":[37.9504,-122.0263],"Victorville, CA":[34.5039,-117.3192],"Santa Rosa, CA":[38.4431,-122.7517],"Vallejo, CA":[38.1483,-122.2493],"Berkeley, CA":[37.8691,-122.2696],"El Monte, CA":[34.0791,-118.0371],"Downey, CA":[33.94,-118.1317],"Costa Mesa, CA":[33.6777,-117.9096],"Inglewood, CA":[33.955,-118.3556],"Carlsbad, CA":[33.1602,-117.325],"Fairfield, CA":[38.2671,-122.0357],"Ventura, CA":[34.2905,-119.2888],"Temecula, CA":[33.4936,-117.1475],"Antioch, CA":[37.9939,-121.8089],"Richmond, CA":[37.94,-122.362],"West Covina, CA":[34.0673,-117.9366],"Murrieta, CA":[33.5631,-117.2738],"Norwalk, CA":[33.9056,-118.0818],"Daly City, CA":[37.7074,-122.4587],"Burbank, CA":[34.1862,-118.3009],"Santa Maria, CA":[34.9545,-120.4325],"El Cajon, CA":[32.7777,-116.9191],"Rialto, CA":[34.1132,-117.3771],"San Mateo, CA":[37.5723,-122.3203],"Compton, CA":[33.8907,-118.239],"Clovis, CA":[36.8243,-119.6824],"Jurupa Valley, CA":[33.9972,-117.4855],"Vista, CA":[33.1694,-117.242],"South Gate, CA":[33.9462,-118.2013],"Mission Viejo, CA":[33.6,-117.6711],"Vacaville, CA":[38.3419,-121.9623],"Carson, CA":[33.823,-118.2684],"Hesperia, CA":[34.4264,-117.3],"Redding, CA":[40.5605,-122.4116],"Santa Monica, CA":[34.0176,-118.4907],"Westminster, CA":[33.7528,-117.9913],"Santa Barbara, CA":[34.4197,-119.7078],"Chico, CA":[39.7565,-121.8518],"Whittier, CA":[34.0011,-118.0371],"Newport Beach, CA":[33.6398,-117.8643],"San Leandro, CA":[37.7205,-122.1587],"Hawthorne, CA":[33.9132,-118.347],"Citrus Heights, CA":[38.6946,-121.2692],"Alhambra, CA":[34.0914,-118.1293],"Tracy, CA":[37.7544,-121.3697],"Livermore, CA":[37.683,-121.763],"Buena Park, CA":[33.8406,-118.0114],"Lakewood, CA":[33.8517,-118.1328],"Merced, CA":[37.3007,-120.4617],"Hemet, CA":[33.7416,-116.973],"Chino, CA":[34.0122,-117.6881],"Menifee, CA":[33.6647,-117.1743],"Lake Forest, CA":[33.64,-117.6882],"Napa, CA":[38.3281,-122.3055],"Redwood City, CA":[37.4647,-122.2304],"Bellflower, CA":[33.8867,-118.1265],"Indio, CA":[33.7219,-116.2357],"Tustin, CA":[33.7382,-117.8207],"Baldwin Park, CA":[34.0842,-117.9695],"Chino Hills, CA":[33.9797,-117.7308],"Mountain View, CA":[37.41,-122.0519],"Alameda, CA":[37.7648,-122.2605],"Upland, CA":[34.1368,-117.6598],"San Ramon, CA":[37.78,-121.9769],"Folsom, CA":[38.6879,-121.1409],"Pleasanton, CA":[37.6658,-121.8755],"Union City, CA":[37.5895,-122.0497],"Perris, CA":[33.7975,-117.28],"Manteca, CA":[37.8088,-121.2186],"Lynwood, CA":[33.9241,-118.2013],"Apple Valley, CA":[34.5291,-117.2132],"Redlands, CA":[34.0397,-117.1804],"Turlock, CA":[37.5036,-120.8505],"Milpitas, CA":[37.4365,-121.8929],"Redondo Beach, CA":[33.8307,-118.3832],"Rancho Cordova, CA":[38.6019,-121.2894],"Yorba Linda, CA":[33.8913,-117.8191],"Palo Alto, CA":[37.4443,-122.1497],"Davis, CA":[38.5548,-121.7485],"Camarillo, CA":[34.2313,-119.0464],"Walnut Creek, CA":[37.8753,-122.0703],"Pittsburg, CA":[38.0169,-121.9082],"South San Francisco, CA":[37.6538,-122.4347],"Yuba City, CA":[39.1286,-121.6216],"San Clemente, CA":[33.4308,-117.6101],"Laguna Niguel, CA":[33.5225,-117.7067],"Pico Rivera, CA":[33.9886,-118.0883],"Montebello, CA":[34.0133,-118.113],"Lodi, CA":[38.1236,-121.263],"Madera, CA":[36.9528,-119.8806],"Santa Cruz, CA":[36.9829,-122.0436],"La Habra, CA":[33.9322,-117.9497],"Encinitas, CA":[33.0369,-117.2911],"Monterey Park, CA":[34.0534,-118.1271],"Tulare, CA":[36.2022,-119.338],"Cupertino, CA":[37.3174,-122.0386],"Gardena, CA":[33.8925,-118.2961],"National City, CA":[32.6749,-117.0897],"Rocklin, CA":[38.7919,-121.2434],"Petaluma, CA":[38.2403,-122.6777],"Huntington Park, CA":[33.9769,-118.2161],"San Rafael, CA":[37.9691,-122.5105],"La Mesa, CA":[32.7604,-117.0115],"Arcadia, CA":[34.1324,-118.0264],"Fountain Valley, CA":[33.7108,-117.9523],"Diamond Bar, CA":[34.0066,-117.8098],"Woodland, CA":[38.6743,-121.7793],"Santee, CA":[32.8486,-116.9862],"Lake Elsinore, CA":[33.6598,-117.3485],"Porterville, CA":[36.0686,-119.0315],"Paramount, CA":[33.8969,-118.1632],"Eastvale, CA":[33.9525,-117.5848],"Rosemead, CA":[34.0658,-118.0853],"Hanford, CA":[36.3314,-119.6491],"Highland, CA":[34.127,-117.2087],"Brentwood, CA":[37.9324,-121.6894],"Novato, CA":[38.1163,-122.5714],"Colton, CA":[34.058,-117.3186],"Cathedral City, CA":[33.8098,-116.4665],"Delano, CA":[35.7715,-119.2459],"Yucaipa, CA":[34.0282,-117.0489],"Watsonville, CA":[36.9205,-121.7634],"Placentia, CA":[33.881,-117.8553],"Glendora, CA":[34.1287,-117.8552],"Gilroy, CA":[37.016,-121.5782],"Palm Desert, CA":[33.7611,-116.3249],"Cerritos, CA":[33.8583,-118.0639],"West Sacramento, CA":[38.5924,-121.5264],"Aliso Viejo, CA":[33.5724,-117.7089],"Poway, CA":[32.9756,-117.0402],"La Mirada, CA":[33.8953,-118.0024],"Rancho Santa Margarita, CA":[33.6518,-117.5884],"Cypress, CA":[33.8186,-118.0387],"Dublin, CA":[37.7166,-121.9226],"Covina, CA":[34.0972,-117.9065],"Azusa, CA":[34.1248,-117.9031],"Palm Springs, CA":[33.8414,-116.5347],"San Luis Obispo, CA":[35.2635,-120.6509],"Ceres, CA":[37.5881,-120.9499],"San Jacinto, CA":[33.7839,-116.9578],"Lincoln, CA":[38.904,-121.2955],"Newark, CA":[37.5368,-122.032],"Lompoc, CA":[34.6583,-120.4506],"El Centro, CA":[32.7893,-115.5665],"Danville, CA":[37.8208,-121.9067],"Bell Gardens, CA":[33.9775,-118.1861],"Coachella, CA":[33.675,-116.1772],"Rancho Palos Verdes, CA":[33.7878,-118.3572],"San Bruno, CA":[37.6247,-122.429],"Rohnert Park, CA":[38.3269,-122.7061],"Brea, CA":[33.9252,-117.8895],"La Puente, CA":[34.0294,-117.9341],"Campbell, CA":[37.28,-121.9554],"San Gabriel, CA":[34.1155,-118.0857],"Beaumont, CA":[33.9504,-116.9701],"Los Banos, CA":[37.0627,-120.8544],"Adelanto, CA":[34.5841,-117.4242],"Culver City, CA":[33.9949,-118.3991],"Calexico, CA":[32.6832,-115.5028],"Stanton, CA":[33.803,-117.9947],"La Quinta, CA":[34.3278,-118.6406],"Monrovia, CA":[34.144,-118.0014],"Martinez, CA":[37.9932,-122.1117],"Hollister, CA":[36.8484,-121.3871],"Denver, CO":[39.8406,-105.008],"Colorado Springs, CO":[38.8335,-104.8206],"Aurora, CO":[39.7378,-104.8152],"Fort Collins, CO":[40.5813,-105.1039],"Lakewood, CO":[39.7047,-105.0814],"Thornton, CO":[39.868,-104.9719],"Arvada, CO":[39.8039,-105.0859],"Westminster, CO":[39.8542,-105.0371],"Pueblo, CO":[38.2879,-104.5848],"Centennial, CO":[39.5807,-104.8772],"Boulder, CO":[40.0497,-105.2143],"Greeley, CO":[40.414,-104.7048],"Longmont, CO":[40.1779,-105.1009],"Loveland, CO":[40.3849,-105.0916],"Broomfield, CO":[39.9245,-105.0609],"Grand Junction, CO":[39.0783,-108.5457],"Castle Rock, CO":[39.3926,-104.8602],"Commerce City, CO":[39.8259,-104.9113],"Parker, CO":[39.5055,-104.7349],"Littleton, CO":[39.5994,-105.0044],"Bridgeport, CT":[41.1669,-73.2052],"New Haven, CT":[41.308,-72.9286],"Hartford, CT":[41.7636,-72.6855],"Stamford, CT":[41.0531,-73.539],"Waterbury, CT":[41.558,-73.0519],"Norwalk, CT":[41.1222,-73.4358],"Danbury, CT":[41.3917,-73.4532],"New Britain, CT":[41.6611,-72.78],"Bristol, CT":[41.6823,-72.9302],"Meriden, CT":[41.5334,-72.7997],"Milford, CT":[41.2175,-73.0549],"West Haven, CT":[41.2701,-72.9638],"Middletown, CT":[41.5569,-72.6652],"Norwich, CT":[41.5371,-72.0849],"Shelton, CT":[41.3047,-73.1294],"Torrington, CT":[41.8131,-73.1156],"Washington, DC":[38.9122,-77.0177],"Wilmington, DE":[39.7378,-75.5497],"Dover, DE":[39.1566,-75.536],"Newark, DE":[39.6349,-75.6993],"Middletown, DE":[39.4815,-75.6832],"Smyrna, DE":[39.2934,-75.6008],"Milford, DE":[38.9218,-75.4299],"Seaford, DE":[38.6404,-75.6041],"Georgetown, DE":[38.679,-75.3932],"Jacksonville, FL":[30.3163,-81.4175],"Miami, FL":[25.779,-80.1982],"Tampa, FL":[28.0879,-82.4593],"Orlando, FL":[28.4988,-80.5825],"St. Petersburg, FL":[27.7723,-82.6386],"Hialeah, FL":[25.905,-80.3049],"Port St. Lucie, FL":[27.2889,-80.298],"Cape Coral, FL":[26.5775,-81.9522],"Tallahassee, FL":[30.4286,-84.2593],"Fort Lauderdale, FL":[26.1216,-80.1288],"Pembroke Pines, FL":[26.0229,-80.2974],"Hollywood, FL":[26.007,-80.1219],"Gainesville, FL":[29.645,-82.31],"Miramar, FL":[25.9861,-80.3036],"Coral Springs, FL":[26.2712,-80.2706],"Palm Bay, FL":[28.0146,-80.5991],"West Palm Beach, FL":[26.714,-80.0659],"Clearwater, FL":[27.9799,-82.7806],"Lakeland, FL":[28.0381,-81.9392],"Pompano Beach, FL":[26.2315,-80.1235],"Miami Gardens, FL":[25.942,-80.2456],"Davie, FL":[26.0765,-80.2521],"Boca Raton, FL":[26.3583,-80.0833],"Sunrise, FL":[26.167,-80.2566],"Deltona, FL":[28.8989,-81.2473],"Plantation, FL":[26.1276,-80.2331],"Palm Coast, FL":[29.5847,-81.208],"Fort Myers, FL":[26.6204,-81.8725],"Largo, FL":[27.9163,-82.7996],"Melbourne, FL":[28.0691,-80.62],"Deerfield Beach, FL":[26.3096,-80.0992],"Boynton Beach, FL":[26.525,-80.0666],"Lauderhill, FL":[26.1404,-80.2134],"Weston, FL":[26.1004,-80.3998],"Kissimmee, FL":[28.3051,-81.4242],"Homestead, FL":[25.4766,-80.4839],"Delray Beach, FL":[26.4564,-80.0793],"Daytona Beach, FL":[29.2012,-81.0371],"Tamarac, FL":[26.2129,-80.2498],"North Miami, FL":[25.8901,-80.1867],"Wellington, FL":[26.6618,-80.2684],"Jupiter, FL":[26.9339,-80.1201],"Ocala, FL":[29.1981,-82.0974],"Port Orange, FL":[29.1214,-80.9767],"Margate, FL":[26.2445,-80.2064],"Coconut Creek, FL":[26.2441,-80.2066],"Sanford, FL":[28.8013,-81.285],"Sarasota, FL":[27.4072,-82.5303],"Pensacola, FL":[30.4223,-87.2248],"Bradenton, FL":[27.5028,-82.5139],"Palm Beach Gardens, FL":[26.8444,-80.0873],"Pinellas Park, FL":[27.8387,-82.7151],"Coral Gables, FL":[25.7215,-80.2684],"Doral, FL":[25.8195,-80.3553],"Bonita Springs, FL":[26.3869,-81.733],"Apopka, FL":[28.6619,-81.4851],"Titusville, FL":[28.5697,-80.8191],"North Port, FL":[27.0781,-82.1735],"Fort Pierce, FL":[27.4382,-80.444],"Winter Haven, FL":[27.9993,-81.7515],"Altamonte Springs, FL":[28.6627,-81.3719],"Cutler Bay, FL":[25.5808,-80.3469],"North Lauderdale, FL":[26.2173,-80.2259],"Oakland Park, FL":[26.1723,-80.132],"Greenacres, FL":[26.6276,-80.1354],"Ormond Beach, FL":[29.2855,-81.0561],"Clermont, FL":[28.5525,-81.7574],"New Smyrna Beach, FL":[29.0247,-80.9584],"Lake Worth, FL":[26.6155,-80.1469],"Winter Garden, FL":[28.4867,-81.6051],"Casselberry, FL":[28.6617,-81.3122],"Atlanta, GA":[33.7498,-84.3169],"Augusta, GA":[33.5117,-82.0995],"Columbus, GA":[32.473,-84.9795],"Macon, GA":[32.8439,-83.5987],"Savannah, GA":[32.0676,-81.1024],"Athens, GA":[33.9761,-83.3632],"Sandy Springs, GA":[33.9304,-84.3733],"Roswell, GA":[34.0408,-84.3859],"Johns Creek, GA":[34.0289,-84.1986],"Albany, GA":[31.5678,-84.1619],"Warner Robins, GA":[32.5934,-83.6416],"Alpharetta, GA":[34.1048,-84.2949],"Marietta, GA":[33.9043,-84.468],"Valdosta, GA":[30.8106,-83.2772],"Smyrna, GA":[33.8796,-84.5023],"Dunwoody, GA":[33.9462,-84.3346],"Rome, GA":[34.2507,-85.1465],"Gainesville, GA":[34.3073,-83.8256],"Peachtree Corners, GA":[33.9699,-84.2215],"Newnan, GA":[33.3894,-84.817],"Douglasville, GA":[33.7494,-84.7459],"Kennesaw, GA":[34.0287,-84.6047],"Lawrenceville, GA":[33.9435,-83.9643],"Statesboro, GA":[32.4408,-81.774],"Duluth, GA":[34.0243,-84.1484],"Stockbridge, GA":[33.5633,-84.2165],"Woodstock, GA":[34.106,-84.5117],"Carrollton, GA":[33.5809,-85.0792],"Honolulu, HI":[21.3095,-157.863],"Hilo, HI":[19.7025,-155.0939],"Kailua, HI":[21.4063,-157.7448],"Kapolei, HI":[21.3453,-158.087],"Kaneohe, HI":[21.4228,-157.8115],"Waipahu, HI":[21.3982,-158.0124],"Pearl City, HI":[21.4084,-157.9652],"Mililani, HI":[21.4531,-158.0174],"Kahului, HI":[20.8814,-156.4783],"Ewa Beach, HI":[21.3274,-158.0103],"Des Moines, IA":[41.6005,-93.6088],"Cedar Rapids, IA":[41.9743,-91.6554],"Davenport, IA":[41.5218,-90.5743],"Sioux City, IA":[42.4972,-96.4029],"Iowa City, IA":[41.6549,-91.5112],"Waterloo, IA":[42.4778,-92.3661],"Council Bluffs, IA":[41.253,-95.881],"Ames, IA":[42.0299,-93.6394],"West Des Moines, IA":[41.5805,-93.7447],"Dubuque, IA":[42.515,-90.6819],"Ankeny, IA":[41.7276,-93.6022],"Urbandale, IA":[41.6295,-93.723],"Cedar Falls, IA":[42.5241,-92.4497],"Marion, IA":[42.0411,-91.5941],"Bettendorf, IA":[41.5509,-90.4942],"Mason City, IA":[43.1499,-93.1954],"Clinton, IA":[41.8517,-90.2078],"Burlington, IA":[40.8087,-91.117],"Boise, ID":[43.6136,-116.2025],"Meridian, ID":[43.615,-116.3975],"Nampa, ID":[43.5834,-116.5848],"Idaho Falls, ID":[43.5177,-111.9906],"Pocatello, ID":[42.8876,-112.4381],"Caldwell, ID":[43.6627,-116.7],"Coeur d'Alene, ID":[47.6777,-116.7805],"Twin Falls, ID":[42.5565,-114.4693],"Post Falls, ID":[47.7205,-116.9353],"Lewiston, ID":[46.3895,-116.9877],"Chicago, IL":[41.8858,-87.6181],"Aurora, IL":[41.7826,-88.2607],"Joliet, IL":[41.5272,-88.0824],"Naperville, IL":[41.7662,-88.141],"Rockford, IL":[42.2922,-89.1161],"Springfield, IL":[39.8,-89.6495],"Elgin, IL":[42.0384,-88.2606],"Peoria, IL":[40.6854,-89.5953],"Champaign, IL":[40.111,-88.2407],"Waukegan, IL":[42.3636,-87.8447],"Cicero, IL":[41.8456,-87.7539],"Bloomington, IL":[40.4783,-88.9893],"Arlington Heights, IL":[42.1116,-87.9791],"Evanston, IL":[42.0546,-87.6943],"Schaumburg, IL":[42.0333,-88.0833],"Bolingbrook, IL":[41.6976,-88.0873],"Palatine, IL":[42.1258,-88.0764],"Skokie, IL":[42.0362,-87.7328],"Des Plaines, IL":[42.0467,-87.8859],"Orland Park, IL":[41.6194,-87.8423],"Tinley Park, IL":[41.5963,-87.8434],"Oak Lawn, IL":[41.7143,-87.7516],"Berwyn, IL":[41.8418,-87.7908],"Mount Prospect, IL":[42.0624,-87.9377],"Normal, IL":[40.5124,-88.9883],"Wheaton, IL":[41.8566,-88.1076],"Hoffman Estates, IL":[42.0481,-88.1047],"Oak Park, IL":[41.8886,-87.7986],"Downers Grove, IL":[41.8034,-88.0138],"Elmhurst, IL":[41.8927,-87.941],"Glenview, IL":[42.0758,-87.8223],"DeKalb, IL":[41.9342,-88.7607],"Lombard, IL":[41.8721,-88.016],"Belleville, IL":[38.5127,-89.9847],"Moline, IL":[41.4906,-90.498],"Buffalo Grove, IL":[42.1598,-87.9644],"Bartlett, IL":[41.9836,-88.1604],"Urbana, IL":[40.1095,-88.2036],"Quincy, IL":[39.9307,-91.3763],"Crystal Lake, IL":[42.2662,-88.3213],"Indianapolis, IN":[39.9384,-86.1389],"Fort Wayne, IN":[41.0716,-85.1367],"Evansville, IN":[37.9746,-87.5674],"South Bend, IN":[41.6727,-86.2535],"Carmel, IN":[39.9712,-86.1245],"Fishers, IN":[39.9573,-85.9457],"Bloomington, IN":[39.1401,-86.5083],"Hammond, IN":[41.6099,-87.5079],"Gary, IN":[41.5933,-87.3464],"Lafayette, IN":[40.4177,-86.8884],"Muncie, IN":[40.1684,-85.3807],"Terre Haute, IN":[39.4667,-87.4068],"Kokomo, IN":[40.4988,-86.1453],"Noblesville, IN":[40.0563,-86.0163],"Anderson, IN":[40.1146,-85.7253],"Greenwood, IN":[39.6224,-86.149],"Elkhart, IN":[41.7101,-85.9729],"Mishawaka, IN":[41.6507,-86.1623],"Lawrence, IN":[39.8387,-86.0253],"Jeffersonville, IN":[38.3078,-85.7359],"Columbus, IN":[39.2055,-85.9317],"Portage, IN":[41.5672,-87.1757],"New Albany, IN":[38.3089,-85.8221],"Richmond, IN":[39.8324,-84.8936],"Valparaiso, IN":[41.4757,-87.0759],"Goshen, IN":[41.5845,-85.838],"Michigan City, IN":[41.698,-86.8699],"Westfield, IN":[40.0489,-86.1499],"Wichita, KS":[37.6898,-97.3415],"Overland Park, KS":[38.9925,-94.6748],"Kansas City, KS":[39.1157,-94.6271],"Olathe, KS":[38.8822,-94.8178],"Topeka, KS":[39.0541,-95.6719],"Lawrence, KS":[38.9644,-95.2418],"Shawnee, KS":[39.0198,-94.7083],"Manhattan, KS":[39.1938,-96.5858],"Lenexa, KS":[38.963,-94.7399],"Salina, KS":[38.8238,-97.6088],"Hutchinson, KS":[38.055,-97.9311],"Leavenworth, KS":[39.3015,-94.9339],"Leawood, KS":[38.9606,-94.6196],"Dodge City, KS":[37.7569,-100.0241],"Garden City, KS":[37.9769,-100.8621],"Emporia, KS":[38.4184,-96.1871],"Louisville, KY":[38.2435,-85.7639],"Lexington, KY":[38.0174,-84.4854],"Bowling Green, KY":[37.0079,-86.4559],"Owensboro, KY":[37.7513,-87.1554],"Covington, KY":[39.0708,-84.5212],"Richmond, KY":[37.7546,-84.2955],"Georgetown, KY":[38.2117,-84.5562],"Florence, KY":[38.9989,-84.6267],"Hopkinsville, KY":[36.8621,-87.4851],"Nicholasville, KY":[37.8806,-84.5731],"Elizabethtown, KY":[37.707,-85.859],"Henderson, KY":[37.8361,-87.59],"Frankfort, KY":[38.1928,-84.8806],"Paducah, KY":[37.0634,-88.6632],"New Orleans, LA":[29.9631,-90.161],"Baton Rouge, LA":[30.4507,-91.187],"Shreveport, LA":[32.5037,-93.7487],"Lafayette, LA":[30.2361,-92.0083],"Lake Charles, LA":[30.2285,-93.188],"Kenner, LA":[29.9912,-90.2479],"Bossier City, LA":[32.5449,-93.7038],"Monroe, LA":[32.5286,-92.1061],"Alexandria, LA":[31.2885,-92.4633],"Houma, LA":[29.5943,-90.7548],"Marrero, LA":[29.8598,-90.1105],"New Iberia, LA":[30.001,-91.82],"Slidell, LA":[30.2784,-89.7712],"Ruston, LA":[32.5308,-92.6439],"Boston, MA":[42.3576,-71.0684],"Worcester, MA":[42.2621,-71.8034],"Springfield, MA":[42.106,-72.5977],"Cambridge, MA":[42.377,-71.1256],"Lowell, MA":[42.656,-71.3051],"Brockton, MA":[42.08,-71.0377],"New Bedford, MA":[41.6347,-70.9372],"Quincy, MA":[42.2491,-70.9978],"Lynn, MA":[42.4634,-70.9455],"Fall River, MA":[41.7182,-71.14],"Newton, MA":[42.3545,-71.1877],"Somerville, MA":[42.3829,-71.1028],"Lawrence, MA":[42.708,-71.1638],"Framingham, MA":[42.3007,-71.4255],"Haverhill, MA":[42.7856,-71.0721],"Waltham, MA":[42.3954,-71.2508],"Malden, MA":[42.4291,-71.0605],"Brookline, MA":[42.3302,-71.1304],"Medford, MA":[42.4183,-71.1067],"Taunton, MA":[41.905,-71.1026],"Chicopee, MA":[42.162,-72.608],"Weymouth, MA":[42.2113,-70.9582],"Revere, MA":[42.4138,-71.0052],"Peabody, MA":[42.5326,-70.9612],"Methuen, MA":[42.728,-71.181],"Barnstable, MA":[41.6983,-70.3001],"Pittsfield, MA":[42.4531,-73.2471],"Attleboro, MA":[41.9296,-71.3009],"Everett, MA":[42.4112,-71.0514],"Salem, MA":[42.5151,-70.9003],"Westfield, MA":[42.1295,-72.7543],"Leominster, MA":[42.5274,-71.7563],"Fitchburg, MA":[42.5796,-71.8031],"Beverly, MA":[42.5608,-70.8759],"Holyoke, MA":[42.202,-72.6262],"Marlborough, MA":[42.3509,-71.5434],"Woburn, MA":[42.4829,-71.1574],"Chelsea, MA":[42.3963,-71.0325],"Baltimore, MD":[39.1718,-76.6483],"Columbia, MD":[39.2141,-76.8788],"Germantown, MD":[39.1704,-77.2699],"Silver Spring, MD":[39.0191,-77.0076],"Waldorf, MD":[38.6371,-76.8778],"Glen Burnie, MD":[39.1677,-76.595],"Ellicott City, MD":[39.2672,-76.7986],"Frederick, MD":[39.4082,-77.4009],"Dundalk, MD":[39.2649,-76.5025],"Rockville, MD":[39.0838,-77.153],"Bethesda, MD":[38.9806,-77.1008],"Gaithersburg, MD":[39.1419,-77.189],"Towson, MD":[39.4025,-76.6032],"Bowie, MD":[38.9797,-76.7435],"Bel Air, MD":[39.5394,-76.3564],"Potomac, MD":[39.0388,-77.1922],"Severn, MD":[39.1275,-76.698],"Hagerstown, MD":[39.632,-77.7372],"Annapolis, MD":[38.9996,-76.5031],"Odenton, MD":[39.0762,-76.6996],"Catonsville, MD":[39.2782,-76.7401],"Salisbury, MD":[38.363,-75.5922],"Portland, ME":[43.6606,-70.2589],"Lewiston, ME":[44.0985,-70.1916],"Bangor, ME":[44.8242,-68.7918],"South Portland, ME":[43.6318,-70.2709],"Auburn, ME":[44.0948,-70.239],"Biddeford, ME":[43.4836,-70.4719],"Sanford, ME":[43.4285,-70.7585],"Saco, ME":[43.5209,-70.4546],"Augusta, ME":[44.3232,-69.7665],"Westbrook, ME":[43.6843,-70.358],"Detroit, MI":[42.3474,-83.0604],"Grand Rapids, MI":[42.9704,-85.6738],"Warren, MI":[42.5159,-82.9824],"Sterling Heights, MI":[42.5648,-83.0701],"Ann Arbor, MI":[42.2794,-83.784],"Lansing, MI":[42.7335,-84.6391],"Flint, MI":[43.0233,-83.6856],"Dearborn, MI":[42.3053,-83.1605],"Livonia, MI":[42.3615,-83.3649],"Troy, MI":[42.5609,-83.1471],"Westland, MI":[42.3189,-83.3749],"Farmington Hills, MI":[42.499,-83.3677],"Kalamazoo, MI":[42.2736,-85.5457],"Wyoming, MI":[42.9009,-85.7058],"Southfield, MI":[42.463,-83.288],"Rochester Hills, MI":[42.6584,-83.1499],"Taylor, MI":[42.2317,-83.2673],"Saint Clair Shores, MI":[42.4635,-82.9007],"Pontiac, MI":[42.668,-83.2893],"Dearborn Heights, MI":[42.2768,-83.2606],"Royal Oak, MI":[42.4906,-83.1366],"Novi, MI":[42.4735,-83.5224],"Battle Creek, MI":[42.3053,-85.1389],"Saginaw, MI":[43.4047,-83.9156],"Kentwood, MI":[42.8695,-85.6447],"East Lansing, MI":[42.7388,-84.4764],"Roseville, MI":[42.5034,-82.9387],"Portage, MI":[42.2075,-85.5957],"Midland, MI":[43.6376,-84.268],"Muskegon, MI":[43.2326,-86.2492],"Lincoln Park, MI":[42.2422,-83.1807],"Bay City, MI":[43.6122,-83.9199],"Jackson, MI":[42.2545,-84.3875],"Holland, MI":[42.7875,-86.1089],"Port Huron, MI":[42.9958,-82.4599],"Minneapolis, MN":[45.0496,-93.2461],"Saint Paul, MN":[44.9027,-93.0964],"Rochester, MN":[44.0496,-92.4896],"Duluth, MN":[47.0944,-91.8467],"Bloomington, MN":[44.8408,-93.2983],"Brooklyn Park, MN":[45.0941,-93.3563],"Plymouth, MN":[45.0105,-93.4555],"Woodbury, MN":[44.9239,-92.9594],"Maple Grove, MN":[45.0725,-93.4558],"Saint Cloud, MN":[45.5521,-94.1284],"Eagan, MN":[44.8041,-93.1669],"Eden Prairie, MN":[44.8574,-93.4376],"Coon Rapids, MN":[45.1732,-93.303],"Blaine, MN":[45.1608,-93.2349],"Burnsville, MN":[44.7678,-93.2775],"Lakeville, MN":[44.6749,-93.2578],"Minnetonka, MN":[44.9138,-93.485],"Apple Valley, MN":[44.7319,-93.2177],"Edina, MN":[44.8897,-93.3499],"Saint Louis Park, MN":[44.9023,-93.371],"Mankato, MN":[44.1538,-93.996],"Moorhead, MN":[46.8677,-96.7572],"Shakopee, MN":[44.7793,-93.5197],"Maplewood, MN":[44.953,-92.9952],"Cottage Grove, MN":[44.8308,-92.9393],"Kansas City, MO":[39.1632,-94.5699],"Saint Louis, MO":[38.6426,-90.3242],"Springfield, MO":[37.2152,-93.295],"Columbia, MO":[38.9382,-92.3049],"Independence, MO":[39.0983,-94.4111],"Lee's Summit, MO":[38.9211,-94.3487],"O'Fallon, MO":[38.8106,-90.6998],"Saint Joseph, MO":[39.7688,-94.8385],"Saint Charles, MO":[38.8014,-90.5065],"Blue Springs, MO":[39.0169,-94.2814],"Saint Peters, MO":[38.7802,-90.6228],"Florissant, MO":[38.8069,-90.3401],"Joplin, MO":[37.0969,-94.5051],"Chesterfield, MO":[38.6318,-90.6142],"Jefferson City, MO":[38.5462,-92.1525],"Cape Girardeau, MO":[37.3169,-89.5459],"Ballwin, MO":[38.6041,-90.5521],"Liberty, MO":[39.2419,-94.4337],"Jackson, MS":[32.2935,-90.1867],"Gulfport, MS":[30.3826,-89.0976],"Southaven, MS":[34.9771,-89.9992],"Hattiesburg, MS":[31.3146,-89.3065],"Biloxi, MS":[30.4035,-88.8971],"Meridian, MS":[32.3574,-88.656],"Tupelo, MS":[34.2538,-88.7209],"Olive Branch, MS":[34.9441,-89.8544],"Greenville, MS":[33.3787,-91.0468],"Horn Lake, MS":[34.9519,-90.0507],"Pearl, MS":[32.2768,-90.1027],"Madison, MS":[32.4671,-90.1087],"Starkville, MS":[33.4501,-88.8176],"Clinton, MS":[32.3411,-90.3229],"Columbus, MS":[33.5377,-88.4262],"Billings, MT":[45.7745,-108.5005],"Missoula, MT":[46.8563,-114.0252],"Great Falls, MT":[47.5098,-111.2734],"Bozeman, MT":[45.6693,-111.0431],"Butte, MT":[45.9916,-112.5178],"Helena, MT":[46.6131,-112.0213],"Kalispell, MT":[48.2209,-114.2892],"Havre, MT":[48.5561,-109.688],"Anaconda, MT":[46.1299,-112.9739],"Belgrade, MT":[45.7801,-111.1439],"Charlotte, NC":[35.2269,-80.8433],"Raleigh, NC":[35.7727,-78.6324],"Greensboro, NC":[36.0726,-79.792],"Durham, NC":[35.9967,-78.8966],"Winston-Salem, NC":[36.0999,-80.2442],"Fayetteville, NC":[35.051,-78.8423],"Cary, NC":[35.7641,-78.7786],"Wilmington, NC":[34.2253,-77.9379],"High Point, NC":[35.9593,-80.0117],"Concord, NC":[35.3716,-80.53],"Asheville, NC":[35.5971,-82.5565],"Greenville, NC":[35.6594,-77.3974],"Gastonia, NC":[35.2449,-81.2194],"Jacksonville, NC":[34.7375,-77.4628],"Chapel Hill, NC":[35.9203,-79.0372],"Rocky Mount, NC":[35.9427,-77.7608],"Huntersville, NC":[35.4106,-80.8431],"Burlington, NC":[36.072,-79.4622],"Wilson, NC":[35.727,-77.9227],"Kannapolis, NC":[35.502,-80.6359],"Apex, NC":[35.7225,-78.8408],"Hickory, NC":[35.7576,-81.3289],"Wake Forest, NC":[35.9815,-78.5392],"Indian Trail, NC":[35.0831,-80.6597],"Mooresville, NC":[35.5774,-80.8226],"Goldsboro, NC":[35.3826,-78.0158],"Monroe, NC":[35.0178,-80.5372],"Salisbury, NC":[35.6515,-80.4889],"Matthews, NC":[35.1219,-80.7136],"New Bern, NC":[35.1019,-77.0319],"Sanford, NC":[35.4641,-79.1764],"Cornelius, NC":[35.4867,-80.8603],"Garner, NC":[35.6813,-78.5975],"Thomasville, NC":[35.8713,-80.0913],"Statesville, NC":[35.8381,-80.8842],"Asheboro, NC":[35.6935,-79.8197],"Kernersville, NC":[36.1165,-80.0831],"Morrisville, NC":[35.8344,-78.8466],"Lumberton, NC":[34.6293,-79.0083],"Fargo, ND":[46.9009,-96.7936],"Bismarck, ND":[46.8234,-100.7748],"Grand Forks, ND":[47.901,-97.0446],"Minot, ND":[48.2291,-101.2985],"West Fargo, ND":[46.8695,-96.895],"Williston, ND":[48.1679,-103.6317],"Dickinson, ND":[46.8873,-102.7876],"Mandan, ND":[46.8306,-100.9092],"Jamestown, ND":[46.9059,-98.7061],"Wahpeton, ND":[46.2651,-96.6133],"Omaha, NE":[41.261,-95.9376],"Lincoln, NE":[40.8169,-96.7103],"Bellevue, NE":[41.1497,-95.9099],"Grand Island, NE":[40.9219,-98.3411],"Kearney, NE":[40.7086,-99.1203],"Fremont, NE":[41.4416,-96.4945],"Hastings, NE":[40.5877,-98.3911],"Norfolk, NE":[42.0329,-97.4229],"North Platte, NE":[41.1326,-100.7746],"Papillion, NE":[41.1523,-96.0371],"Manchester, NH":[42.9929,-71.4633],"Nashua, NH":[42.7564,-71.4667],"Concord, NH":[43.2185,-71.5277],"Derry, NH":[42.8874,-71.302],"Dover, NH":[43.19,-70.8849],"Rochester, NH":[43.2684,-70.9766],"Salem, NH":[42.7846,-71.2176],"Merrimack, NH":[42.8667,-71.5128],"Londonderry, NH":[42.8656,-71.3772],"Hudson, NH":[42.769,-71.4121],"Keene, NH":[42.9431,-72.2895],"Portsmouth, NH":[43.0665,-70.7804],"Newark, NJ":[40.7308,-74.1744],"Jersey City, NJ":[40.7164,-74.038],"Paterson, NJ":[40.9143,-74.1671],"Elizabeth, NJ":[40.6717,-74.2043],"Edison, NJ":[40.5171,-74.3973],"Woodbridge, NJ":[40.556,-74.2845],"Lakewood, NJ":[40.085,-74.2042],"Toms River, NJ":[39.9771,-74.1565],"Trenton, NJ":[40.2169,-74.7433],"Clifton, NJ":[40.8789,-74.1425],"Camden, NJ":[39.9258,-75.12],"Brick, NJ":[40.0408,-74.1269],"Cherry Hill, NJ":[39.9308,-75.0175],"Passaic, NJ":[40.8601,-74.1283],"Middletown, NJ":[40.396,-74.1139],"Union City, NJ":[40.7682,-74.0306],"Old Bridge, NJ":[40.398,-74.3236],"East Orange, NJ":[40.7696,-74.2077],"Bayonne, NJ":[40.6664,-74.1192],"Franklin, NJ":[41.1164,-74.5865],"North Bergen, NJ":[40.793,-74.0177],"Vineland, NJ":[39.4818,-75.0091],"Union, NJ":[40.6952,-74.2677],"Piscataway, NJ":[40.5515,-74.459],"New Brunswick, NJ":[40.4891,-74.4482],"Jackson, NJ":[40.121,-74.3017],"Wayne, NJ":[40.9471,-74.2466],"Irvington, NJ":[40.7261,-74.2313],"Parsippany, NJ":[40.8621,-74.4117],"Howell, NJ":[40.1481,-74.2137],"Perth Amboy, NJ":[40.5176,-74.2754],"Hoboken, NJ":[40.7445,-74.0329],"Plainfield, NJ":[40.6198,-74.4253],"West New York, NJ":[40.7882,-74.0129],"Washington, NJ":[40.7582,-74.9914],"East Brunswick, NJ":[40.4284,-74.4064],"Bloomfield, NJ":[40.8035,-74.1891],"West Orange, NJ":[40.7859,-74.2568],"Albuquerque, NM":[35.0936,-106.6423],"Las Cruces, NM":[32.3216,-106.746],"Rio Rancho, NM":[35.2493,-106.6818],"Santa Fe, NM":[35.7025,-105.9748],"Roswell, NM":[33.3885,-104.5259],"Farmington, NM":[36.7065,-108.1995],"Clovis, NM":[34.4126,-103.2214],"Hobbs, NM":[32.7222,-103.1372],"Alamogordo, NM":[32.8932,-105.9485],"Carlsbad, NM":[32.4119,-104.2395],"Gallup, NM":[35.5065,-108.7414],"Los Lunas, NM":[34.7806,-106.7115],"Deming, NM":[32.2318,-107.7466],"Las Vegas, NV":[36.1721,-115.1224],"Henderson, NV":[35.9927,-114.9517],"Reno, NV":[39.5268,-119.8113],"North Las Vegas, NV":[36.4475,-114.8514],"Sparks, NV":[39.5473,-119.7556],"Carson City, NV":[39.1507,-119.7459],"Fernley, NV":[39.6019,-119.235],"Elko, NV":[40.8262,-115.7247],"Mesquite, NV":[36.8055,-114.0663],"Boulder City, NV":[35.9727,-114.8344],"New York, NY":[40.7484,-73.9967],"Buffalo, NY":[42.8967,-78.8846],"Yonkers, NY":[40.9407,-73.8883],"Rochester, NY":[43.1683,-77.6026],"Syracuse, NY":[43.0459,-76.1528],"Albany, NY":[42.6525,-73.7566],"New Rochelle, NY":[40.9166,-73.7877],"Mount Vernon, NY":[40.9079,-73.838],"Schenectady, NY":[42.8155,-73.9395],"Utica, NY":[43.0871,-75.2315],"White Plains, NY":[41.033,-73.7652],"Hempstead, NY":[40.7139,-73.6003],"Troy, NY":[42.7438,-73.6937],"Niagara Falls, NY":[43.0955,-79.0414],"Binghamton, NY":[42.1463,-75.8865],"Freeport, NY":[40.6536,-73.5866],"Valley Stream, NY":[40.6742,-73.7057],"Long Beach, NY":[40.5877,-73.6595],"Rome, NY":[43.2193,-75.4498],"North Tonawanda, NY":[43.0498,-78.851],"Ithaca, NY":[42.4485,-76.4929],"Poughkeepsie, NY":[41.7021,-73.9218],"Jamestown, NY":[42.0928,-79.244],"Elmira, NY":[42.1008,-76.812],"Newburgh, NY":[41.5178,-74.036],"Middletown, NY":[41.4572,-74.412],"Auburn, NY":[42.93,-76.5626],"Watertown, NY":[43.9743,-75.9122],"Glen Cove, NY":[40.865,-73.6277],"Saratoga Springs, NY":[43.0801,-73.7806],"Kingston, NY":[41.9301,-74.0236],"Peekskill, NY":[41.2937,-73.9026],"Lockport, NY":[43.16,-78.6923],"Plattsburgh, NY":[44.6927,-73.466],"Cortland, NY":[42.5952,-76.1857],"Oswego, NY":[43.4438,-76.4975],"Beacon, NY":[41.5097,-73.9634],"Batavia, NY":[43.0003,-78.1929],"Ellenville, NY":[41.7218,-74.4141],"Monticello, NY":[41.6516,-74.7007],"Liberty, NY":[41.7962,-74.7484],"Newark, NY":[43.0519,-77.0946],"Geneva, NY":[42.8637,-76.9913],"Canandaigua, NY":[42.8689,-77.2846],"Oneonta, NY":[42.4625,-75.0491],"Amsterdam, NY":[42.9488,-74.1839],"Gloversville, NY":[43.0616,-74.3375],"Johnstown, NY":[43.0069,-74.3715],"Hudson, NY":[42.247,-73.7552],"Catskill, NY":[42.2276,-73.8985],"Columbus, OH":[40.1444,-82.9789],"Cleveland, OH":[41.4918,-81.6757],"Cincinnati, OH":[39.0913,-84.2774],"Toledo, OH":[41.642,-83.5438],"Akron, OH":[41.0449,-81.52],"Dayton, OH":[39.7654,-84.0998],"Parma, OH":[41.4048,-81.7229],"Canton, OH":[40.827,-81.3853],"Youngstown, OH":[41.0986,-80.6474],"Lorain, OH":[41.4578,-82.171],"Hamilton, OH":[39.4059,-84.5221],"Springfield, OH":[39.9242,-83.8089],"Kettering, OH":[39.6895,-84.1688],"Elyria, OH":[41.3724,-82.1051],"Lakewood, OH":[41.4827,-81.7971],"Cuyahoga Falls, OH":[41.1401,-81.479],"Middletown, OH":[39.5321,-84.3896],"Euclid, OH":[41.5696,-81.5257],"Newark, OH":[40.0724,-82.4046],"Mansfield, OH":[40.7633,-82.5138],"Mentor, OH":[41.6895,-81.3421],"Beavercreek, OH":[39.7092,-84.0633],"Cleveland Heights, OH":[41.5201,-81.5562],"Strongsville, OH":[41.3132,-81.8285],"Dublin, OH":[40.0992,-83.1142],"Fairfield, OH":[39.3266,-84.5479],"Findlay, OH":[41.0442,-83.65],"Warren, OH":[41.1724,-80.8718],"Lancaster, OH":[39.7187,-82.6031],"Lima, OH":[40.7641,-84.0973],"Huber Heights, OH":[39.8439,-84.1247],"Westerville, OH":[40.1545,-82.9097],"Marion, OH":[40.5886,-83.1286],"Grove City, OH":[39.8814,-83.0839],"Reynoldsburg, OH":[39.9551,-82.8035],"Delaware, OH":[40.2932,-83.0723],"Brunswick, OH":[41.2471,-81.828],"Stow, OH":[41.1748,-81.438],"Oklahoma City, OK":[35.3337,-97.4922],"Tulsa, OK":[36.0557,-96.0602],"Norman, OK":[35.2212,-97.4448],"Broken Arrow, OK":[35.9908,-95.8143],"Lawton, OK":[34.5915,-98.3698],"Edmond, OK":[35.68,-97.53],"Moore, OK":[35.3395,-97.4867],"Midwest City, OK":[35.4495,-97.3967],"Enid, OK":[36.4028,-97.8623],"Stillwater, OK":[36.1043,-97.0609],"Muskogee, OK":[35.7307,-95.3755],"Bartlesville, OK":[36.744,-95.9921],"Owasso, OK":[36.2863,-95.8222],"Shawnee, OK":[35.3491,-96.9313],"Ponca City, OK":[36.7031,-97.0784],"Ardmore, OK":[34.1767,-97.1342],"Duncan, OK":[34.5073,-97.9403],"Yukon, OK":[35.5067,-97.7622],"Bixby, OK":[35.9173,-95.8729],"Portland, OR":[45.4429,-122.6151],"Salem, OR":[44.926,-122.9797],"Eugene, OR":[44.0737,-123.0788],"Gresham, OR":[45.5154,-122.4203],"Hillsboro, OR":[45.4984,-122.957],"Beaverton, OR":[45.475,-122.8054],"Bend, OR":[44.0928,-121.2936],"Medford, OR":[42.3193,-122.887],"Springfield, OR":[44.0611,-123.0153],"Corvallis, OR":[44.5904,-123.2722],"Albany, OR":[44.6277,-123.0944],"Tigard, OR":[45.4312,-122.7715],"Lake Oswego, OR":[45.4093,-122.6847],"Keizer, OR":[44.9903,-123.025],"Grants Pass, OR":[42.4638,-123.3457],"Oregon City, OR":[45.3377,-122.57],"McMinnville, OR":[45.2097,-123.2043],"Redmond, OR":[44.2767,-121.1896],"Tualatin, OR":[45.3727,-122.7631],"West Linn, OR":[45.3669,-122.648],"Woodburn, OR":[45.1446,-122.8583],"Newberg, OR":[45.3099,-122.9685],"Forest Grove, OR":[45.5328,-123.1152],"Roseburg, OR":[43.2227,-123.3664],"Klamath Falls, OR":[42.2296,-121.787],"Ashland, OR":[42.1885,-122.693],"Philadelphia, PA":[39.865,-75.2752],"Pittsburgh, PA":[40.4745,-79.9525],"Allentown, PA":[40.6027,-75.471],"Erie, PA":[42.126,-80.086],"Reading, PA":[40.3466,-75.9351],"Scranton, PA":[41.3731,-75.6841],"Bethlehem, PA":[40.6335,-75.3952],"Lancaster, PA":[40.0754,-76.3199],"Harrisburg, PA":[40.2618,-76.8831],"York, PA":[39.9635,-76.7269],"Altoona, PA":[40.5209,-78.4089],"Wilkes-Barre, PA":[41.2459,-75.8813],"Chester, PA":[39.8498,-75.3747],"Williamsport, PA":[41.2472,-77.0206],"Easton, PA":[40.7533,-75.2517],"Lebanon, PA":[40.3359,-76.4259],"Hazleton, PA":[40.9621,-75.9782],"New Castle, PA":[40.9922,-80.3284],"Johnstown, PA":[40.326,-78.9141],"Norristown, PA":[40.0959,-75.3733],"McKeesport, PA":[40.3411,-79.8105],"Chambersburg, PA":[39.9313,-77.6579],"Carlisle, PA":[40.2039,-77.1995],"Hanover, PA":[39.7943,-76.9812],"Pottstown, PA":[40.2453,-75.65],"Sharon, PA":[41.2316,-80.4993],"Bloomsburg, PA":[41.0115,-76.4384],"West Chester, PA":[39.9845,-75.5962],"Butler, PA":[40.8621,-79.9027],"Washington, PA":[40.1717,-80.256],"Meadville, PA":[41.6338,-80.1488],"Indiana, PA":[40.6196,-79.1596],"Greensburg, PA":[40.3074,-79.5424],"Uniontown, PA":[39.8897,-79.7282],"Oil City, PA":[41.4319,-79.6916],"Warren, PA":[41.8453,-79.1429],"Bradford, PA":[41.9547,-78.654],"Sunbury, PA":[40.8551,-76.7776],"Lock Haven, PA":[41.1425,-77.4436],"Pottsville, PA":[40.684,-76.2123],"Providence, RI":[41.8255,-71.4114],"Warwick, RI":[41.7026,-71.4476],"Cranston, RI":[41.7766,-71.4383],"Pawtucket, RI":[41.8729,-71.3907],"East Providence, RI":[41.8138,-71.3688],"Woonsocket, RI":[41.9995,-71.5137],"Newport, RI":[41.5045,-71.3035],"Central Falls, RI":[41.8883,-71.3945],"Westerly, RI":[41.3691,-71.8126],"Bristol, RI":[41.6825,-71.2676],"Charleston, SC":[32.9622,-79.8653],"Columbia, SC":[34.0726,-81.1796],"North Charleston, SC":[33.0562,-80.0759],"Mount Pleasant, SC":[32.8162,-79.852],"Rock Hill, SC":[34.9151,-81.0129],"Greenville, SC":[34.8472,-82.406],"Summerville, SC":[33.028,-80.1739],"Sumter, SC":[33.9282,-80.321],"Goose Creek, SC":[32.9887,-80.0199],"Hilton Head Island, SC":[32.1632,-80.7533],"Florence, SC":[34.1838,-79.7728],"Spartanburg, SC":[34.9352,-81.9654],"Myrtle Beach, SC":[33.7587,-78.8044],"Aiken, SC":[33.553,-81.7194],"Anderson, SC":[34.5261,-82.6304],"Greer, SC":[34.8968,-82.2674],"Mauldin, SC":[34.7807,-82.3035],"Greenwood, SC":[34.1758,-82.1562],"North Augusta, SC":[33.5178,-81.9348],"Easley, SC":[34.829,-82.5796],"Sioux Falls, SD":[43.488,-96.7343],"Rapid City, SD":[44.077,-103.2003],"Aberdeen, SD":[45.4661,-98.4856],"Brookings, SD":[44.3056,-96.7914],"Watertown, SD":[44.9043,-97.124],"Mitchell, SD":[43.7109,-98.027],"Yankton, SD":[42.8821,-97.3986],"Pierre, SD":[44.3695,-100.3211],"Huron, SD":[44.359,-98.2163],"Vermillion, SD":[42.7951,-96.9258],"Nashville, TN":[36.167,-86.7784],"Memphis, TN":[35.0337,-89.9343],"Knoxville, TN":[35.9609,-83.9189],"Chattanooga, TN":[35.0455,-85.3081],"Clarksville, TN":[36.522,-87.349],"Murfreesboro, TN":[35.7913,-86.357],"Franklin, TN":[35.9328,-86.8788],"Jackson, TN":[35.6102,-88.814],"Johnson City, TN":[36.3339,-82.3408],"Bartlett, TN":[35.2045,-89.874],"Hendersonville, TN":[36.3054,-86.6072],"Kingsport, TN":[36.5528,-82.554],"Collierville, TN":[35.0551,-89.6767],"Smyrna, TN":[35.9656,-86.5048],"Cleveland, TN":[35.1313,-84.875],"Brentwood, TN":[36.0331,-86.7828],"Germantown, TN":[35.0883,-89.8053],"Columbia, TN":[35.6156,-87.038],"La Vergne, TN":[36.0127,-86.56],"Gallatin, TN":[36.3834,-86.4512],"Cookeville, TN":[36.1743,-85.4953],"Mount Juliet, TN":[36.2,-86.5186],"Lebanon, TN":[36.2098,-86.3024],"Morristown, TN":[36.1957,-83.2755],"Oak Ridge, TN":[36.0159,-84.2623],"Maryville, TN":[35.7824,-83.9145],"Bristol, TN":[36.5686,-82.1819],"Houston, TX":[29.5962,-95.4587],"San Antonio, TX":[29.4685,-98.5264],"Dallas, TX":[32.9968,-96.7921],"Austin, TX":[30.2107,-97.9427],"Fort Worth, TX":[32.7469,-97.3268],"El Paso, TX":[31.7584,-106.4783],"Arlington, TX":[32.6336,-97.1469],"Corpus Christi, TX":[27.7941,-97.403],"Plano, TX":[33.055,-96.7365],"Laredo, TX":[27.5155,-99.4986],"Lubbock, TX":[33.5865,-101.8606],"Garland, TX":[32.9227,-96.6248],"Irving, TX":[32.842,-96.9719],"Amarillo, TX":[35.2032,-101.8421],"Grand Prairie, TX":[32.7649,-97.0112],"Brownsville, TX":[25.9337,-97.5174],"McKinney, TX":[33.1966,-96.6085],"Frisco, TX":[33.1506,-96.8233],"Pasadena, TX":[29.692,-95.2005],"Killeen, TX":[31.117,-97.7261],"McAllen, TX":[26.2154,-98.2359],"Mesquite, TX":[32.7678,-96.6082],"Midland, TX":[31.9896,-102.0626],"Denton, TX":[33.2289,-97.1314],"Waco, TX":[31.5525,-97.1396],"Carrollton, TX":[32.9657,-96.8825],"Round Rock, TX":[30.5145,-97.668],"Abilene, TX":[32.4682,-99.7182],"Pearland, TX":[29.5617,-95.2721],"Richardson, TX":[32.966,-96.7452],"Odessa, TX":[31.8465,-102.3663],"Sugar Land, TX":[29.6342,-95.6219],"College Station, TX":[30.6045,-96.3123],"Beaumont, TX":[30.0688,-94.1039],"Lewisville, TX":[33.0461,-96.9939],"Tyler, TX":[32.3254,-95.2922],"League City, TX":[29.5173,-95.0963],"San Angelo, TX":[31.4782,-100.4818],"Allen, TX":[33.0934,-96.6454],"Wichita Falls, TX":[33.9053,-98.4976],"Longview, TX":[32.5269,-94.7233],"Edinburg, TX":[26.3042,-98.1569],"Mission, TX":[26.2415,-98.3426],"Bryan, TX":[30.6327,-96.3662],"Baytown, TX":[29.7461,-94.9653],"Pharr, TX":[26.1771,-98.187],"Temple, TX":[31.0895,-97.3343],"Missouri City, TX":[29.5704,-95.5423],"Flower Mound, TX":[33.0238,-97.1044],"Harlingen, TX":[26.1951,-97.689],"North Richland Hills, TX":[32.854,-97.2207],"Victoria, TX":[28.809,-96.9993],"Conroe, TX":[30.3125,-95.4527],"New Braunfels, TX":[29.6947,-98.113],"Mansfield, TX":[32.5773,-97.1416],"Cedar Park, TX":[30.4772,-97.8176],"Rowlett, TX":[32.9027,-96.5636],"Port Arthur, TX":[29.8826,-93.9626],"Euless, TX":[32.8582,-97.0832],"Georgetown, TX":[30.633,-97.6707],"Pflugerville, TX":[30.4421,-97.6299],"DeSoto, TX":[32.5932,-96.8547],"San Marcos, TX":[29.8754,-97.9404],"Grapevine, TX":[32.9314,-97.0962],"Bedford, TX":[32.8536,-97.1358],"Galveston, TX":[29.2983,-94.793],"Cedar Hill, TX":[32.5885,-96.9438],"Texas City, TX":[29.397,-94.9203],"Wylie, TX":[33.0041,-96.5394],"Haltom City, TX":[32.8087,-97.2709],"Keller, TX":[32.9344,-97.2514],"Coppell, TX":[32.9673,-96.9805],"Rockwall, TX":[32.9311,-96.4594],"Huntsville, TX":[30.7947,-95.5337],"Duncanville, TX":[32.6587,-96.9114],"Sherman, TX":[33.6435,-96.6075],"The Colony, TX":[33.094,-96.8836],"Burleson, TX":[32.5316,-97.309],"Hurst, TX":[32.8211,-97.1756],"Lancaster, TX":[32.6161,-96.783],"Texarkana, TX":[33.4074,-94.1182],"Friendswood, TX":[29.5224,-95.1879],"Weslaco, TX":[26.1694,-97.9887],"Canutillo, TX":[31.9344,-106.5929],"San Elizario, TX":[31.585,-106.2722],"Anthony, TX":[31.9907,-106.5976],"Fabens, TX":[31.5022,-106.1581],"Clint, TX":[31.5494,-106.2038],"Salt Lake City, UT":[40.7559,-111.8967],"West Valley City, UT":[40.6916,-112.0011],"Provo, UT":[40.2319,-111.6755],"West Jordan, UT":[40.6254,-111.9677],"Orem, UT":[40.3134,-111.6953],"Sandy, UT":[40.5794,-111.8816],"Ogden, UT":[41.2443,-112.0072],"St. George, UT":[37.1067,-113.5953],"Layton, UT":[41.0846,-111.9274],"South Jordan, UT":[40.5219,-111.9383],"Lehi, UT":[40.3958,-111.8506],"Taylorsville, UT":[40.6677,-111.9388],"Logan, UT":[41.747,-111.8226],"Murray, UT":[40.6669,-111.888],"Draper, UT":[40.5046,-111.881],"Bountiful, UT":[40.8775,-111.8727],"Riverton, UT":[40.5379,-111.9547],"Herriman, UT":[40.5032,-112.034],"Spanish Fork, UT":[40.1099,-111.6462],"Roy, UT":[41.1724,-112.0382],"Pleasant Grove, UT":[40.372,-111.7333],"Tooele, UT":[40.5454,-112.3002],"Springville, UT":[40.1625,-111.5987],"Virginia Beach, VA":[36.8527,-75.9783],"Chesapeake, VA":[36.7352,-76.2384],"Norfolk, VA":[36.8466,-76.2855],"Arlington, VA":[38.8871,-77.0932],"Richmond, VA":[37.4532,-77.4698],"Newport News, VA":[37.058,-76.4607],"Alexandria, VA":[38.82,-77.0589],"Hampton, VA":[37.0065,-76.413],"Roanoke, VA":[37.2725,-79.953],"Portsmouth, VA":[36.8089,-76.3671],"Suffolk, VA":[36.8668,-76.5598],"Lynchburg, VA":[37.3862,-79.1715],"Harrisonburg, VA":[38.4489,-78.8714],"Charlottesville, VA":[38.0548,-78.4909],"Danville, VA":[36.6218,-79.4124],"Manassas, VA":[38.7518,-77.4728],"Petersburg, VA":[37.22,-77.4326],"Fredericksburg, VA":[38.2995,-77.4772],"Winchester, VA":[39.1581,-78.2313],"Salem, VA":[37.2853,-80.0692],"Staunton, VA":[38.1451,-79.0752],"Herndon, VA":[38.9764,-77.3839],"Hopewell, VA":[37.2876,-77.295],"Fairfax, VA":[38.8604,-77.2649],"Waynesboro, VA":[38.0774,-78.9035],"Blacksburg, VA":[37.2288,-80.4273],"Christiansburg, VA":[37.1297,-80.4092],"Radford, VA":[37.1358,-80.5717],"Bristol, VA":[36.6181,-82.1823],"Martinsville, VA":[36.6871,-79.8691],"Burlington, VT":[44.484,-73.2199],"South Burlington, VT":[44.4513,-73.1796],"Rutland, VT":[43.6141,-72.9708],"Barre, VT":[44.1945,-72.4936],"Montpelier, VT":[44.2574,-72.5698],"Winooski, VT":[44.4949,-73.1874],"St. Albans, VT":[44.8111,-73.089],"Newport, VT":[44.9393,-72.2065],"Vergennes, VT":[44.1326,-73.2793],"Middlebury, VT":[44.007,-73.1661],"Seattle, WA":[47.6114,-122.3305],"Spokane, WA":[47.6665,-117.4365],"Tacoma, WA":[47.2764,-122.7583],"Vancouver, WA":[45.6418,-122.6801],"Bellevue, WA":[47.6199,-122.2074],"Kent, WA":[47.3695,-122.1949],"Everett, WA":[47.9884,-122.2006],"Renton, WA":[47.4648,-122.2075],"Federal Way, WA":[47.3203,-122.3117],"Spokane Valley, WA":[47.6732,-117.2394],"Yakima, WA":[46.607,-120.4773],"Kirkland, WA":[47.6786,-122.1894],"Bellingham, WA":[48.749,-122.4887],"Kennewick, WA":[46.2109,-119.168],"Auburn, WA":[47.3163,-122.2701],"Pasco, WA":[46.2492,-119.1044],"Marysville, WA":[48.0656,-122.1562],"Lakewood, WA":[47.1229,-122.5293],"Redmond, WA":[47.6718,-122.1232],"Shoreline, WA":[47.7557,-122.3415],"Richland, WA":[46.2833,-119.2892],"Sammamish, WA":[47.6244,-122.0423],"Burien, WA":[47.4704,-122.3468],"Olympia, WA":[47.0129,-122.8763],"Lacey, WA":[47.024,-122.7827],"Edmonds, WA":[47.8007,-122.3669],"Puyallup, WA":[47.1991,-122.3151],"Bremerton, WA":[47.6019,-122.6299],"Longview, WA":[46.1514,-122.9634],"Wenatchee, WA":[47.4253,-120.3273],"Mount Vernon, WA":[48.4164,-122.3265],"Walla Walla, WA":[46.0614,-118.3315],"Pullman, WA":[46.7352,-117.1729],"Bothell, WA":[47.7497,-122.2159],"Issaquah, WA":[47.5509,-122.0335],"Mercer Island, WA":[47.5631,-122.2266],"Milwaukee, WI":[43.0343,-87.9151],"Madison, WI":[43.073,-89.3817],"Green Bay, WI":[44.4853,-88.0169],"Kenosha, WI":[42.6052,-87.8299],"Racine, WI":[42.6868,-87.8378],"Appleton, WI":[44.2773,-88.3976],"Waukesha, WI":[42.9993,-88.2196],"Eau Claire, WI":[44.784,-91.4877],"Oshkosh, WI":[44.022,-88.5436],"Janesville, WI":[42.6915,-89.0331],"West Allis, WI":[43.0167,-88.007],"La Crosse, WI":[43.7989,-91.2175],"Sheboygan, WI":[43.741,-87.7247],"Wauwatosa, WI":[43.0495,-88.0076],"Fond du Lac, WI":[43.7704,-88.4291],"New Berlin, WI":[42.974,-88.1553],"Wausau, WI":[44.9634,-89.634],"Brookfield, WI":[43.0622,-88.098],"Beloit, WI":[42.5229,-89.0399],"Greenfield, WI":[42.9614,-88.0126],"Manitowoc, WI":[44.0971,-87.6823],"Sun Prairie, WI":[43.1869,-89.2227],"Superior, WI":[46.7016,-92.0912],"Stevens Point, WI":[44.5212,-89.5588],"Neenah, WI":[44.1811,-88.4792],"Charleston, WV":[38.349,-81.6306],"Huntington, WV":[38.4097,-82.4423],"Morgantown, WV":[39.6368,-80.018],"Parkersburg, WV":[39.2644,-81.5354],"Wheeling, WV":[40.0727,-80.6851],"Martinsburg, WV":[39.46,-77.9589],"Fairmont, WV":[39.4727,-80.146],"Beckley, WV":[37.7932,-81.2061],"Clarksburg, WV":[39.2784,-80.3487],"Weirton, WV":[40.4137,-80.5683],"Bluefield, WV":[37.2798,-81.229],"Hurricane, WV":[38.4257,-81.9943],"Cheyenne, WY":[41.1437,-104.7962],"Casper, WY":[42.8458,-106.3166],"Laramie, WY":[41.3129,-105.5811],"Gillette, WY":[44.282,-105.4974],"Rock Springs, WY":[41.606,-109.23],"Sheridan, WY":[44.7849,-106.9648],"Green River, WY":[41.5196,-109.4714],"Evanston, WY":[41.2609,-110.9631],"Riverton, WY":[43.0458,-108.4113],"Cody, WY":[44.5231,-109.0756]}""")


def city_point(city: str) -> tuple[float, float] | None:
    """Where a city is, if we already know without asking anyone."""
    got = CITY_COORDS.get(city)
    return (got[0], got[1]) if got else None


def us_cities(first_state: str = "") -> list[str]:
    """Every city in the country as "City, ST", the given state first.

    The order is the whole point: their own state before anywhere else, and
    the biggest cities of each state before its smaller ones, so the crawl
    starts where they actually are and works outwards.
    """
    states = sorted(US_CITIES_BY_STATE)
    first = (first_state or "").upper()
    if first in US_CITIES_BY_STATE:
        states.remove(first)
        states.insert(0, first)
    out = []
    for state in states:
        for city in US_CITIES_BY_STATE[state].split(","):
            city = city.strip()
            if city:
                out.append(f"{city}, {state}")
    return out


DEFAULT_TRADES = [
    "plumbers", "electricians", "landscapers", "tree service", "roofers",
    "handyman", "house cleaning", "towing", "junk removal", "septic service",
    "masonry", "excavation", "snow plowing", "small engine repair",
    "auto repair", "barber shops", "moving companies", "pest control",
    "HVAC", "fencing contractors",
]


def call_opener(lead: dict, cfg: dict) -> str:
    """A short thing to say when they answer. Plain, honest, and no API needed.

    Deliberately not generated: it should read the same every time so it can be
    practised, and it has to work before any keys are in.
    """
    name = (cfg.get("your_name") or "").strip() or "me"
    studio = (cfg.get("studio_name") or "Solo Studio").strip()
    price = fmt_price(cfg.get("site_price_usd", 500))
    business = lead.get("name") or "your business"
    return (
        f"Hi, is this {business}? My name's {name}, I run {studio} — I build "
        f"websites for local businesses.\n\n"
        f"I noticed you don't have a website up. I can put together a simple "
        f"one-page site for a flat ${price} — no monthly fee.\n\n"
        f"What I'd normally do is design it first so you can see it, and you "
        f"only pay if you like it. What's the best email to send it to?"
    )


def fmt_price(value) -> str:
    """500.0 -> '500', 499.5 -> '499.50' (for email/UI copy)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(v)) if v.is_integer() else f"{v:.2f}"

# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

STAGE_FOUND = "found"                      # discovered via Places, may lack email
STAGE_CONTACTED = "contacted"              # cold email sent, waiting for reply
STAGE_BUILDING_PREVIEW = "building_preview"    # transient: generating + deploying preview
STAGE_PREVIEW_SENT = "preview_sent"        # watermarked preview emailed
STAGE_SENDING_PAYMENT_LINK = "sending_payment_link"  # transient
STAGE_PAYMENT_LINK_SENT = "payment_link_sent"  # checkout link emailed, polling Stripe
STAGE_PAID = "paid"                        # Stripe verified paid; delivery pending
STAGE_DEPLOYING_FINAL = "deploying_final"  # transient: deploying clean site
STAGE_DELIVERED = "delivered"              # clean site live + link emailed
STAGE_NOT_INTERESTED = "not_interested"    # declined or unsubscribed
STAGE_ERROR = "error"                      # gave up after repeated failures

ALL_STAGES = [
    STAGE_FOUND, STAGE_CONTACTED, STAGE_BUILDING_PREVIEW, STAGE_PREVIEW_SENT,
    STAGE_SENDING_PAYMENT_LINK, STAGE_PAYMENT_LINK_SENT, STAGE_PAID,
    STAGE_DEPLOYING_FINAL, STAGE_DELIVERED, STAGE_NOT_INTERESTED, STAGE_ERROR,
]

# Transient stages resume automatically each tick (idempotently).
TRANSIENT_STAGES = {
    STAGE_BUILDING_PREVIEW, STAGE_SENDING_PAYMENT_LINK, STAGE_DEPLOYING_FINAL,
}

MAX_ATTEMPTS = 5  # per transient stage before parking the lead in 'error'


# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

def app_data_dir() -> str:
    """Directory for config.json, the database, and logs.

    Resolution order:
      1. SOLO_STUDIO_HOME env var (used by tests; not required in normal use)
      2. a config.json sitting next to this file (portable/dev layout)
      3. ~/Library/Application Support/Solo Studio on macOS, ~/.solo-studio elsewhere
    """
    env = os.environ.get("SOLO_STUDIO_HOME")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, "config.json")):
        return here
    if sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/Solo Studio")
    else:
        d = os.path.expanduser("~/.solo-studio")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(app_data_dir(), "config.json")


DEFAULT_CONFIG = {
    # API keys — all filled in via the dashboard Setup page.
    "google_places_api_key": "",
    "inkbox_api_key": "",
    "inkbox_agent_handle": "",
    "anthropic_api_key": "",
    "anthropic_model": "claude-opus-5",
    "netlify_api_key": "",
    # Optional extras. Everything works without them; each one widens the net.
    "yelp_api_key": "",       # a fourth index of local businesses
    "hunter_api_key": "",     # addresses behind a domain we already know
    "stripe_secret_key": "",
    # Business / outreach settings.
    "your_name": "",
    "studio_name": "Solo Studio",
    "mailing_address": "",       # physical address, required in cold email (CAN-SPAM)
    "site_price_usd": 500,
    "currency": "usd",
    "outreach_subject": "A website for {lead_name}",
    "outreach_body": (
        "Hi,\n\n"
        "I came across {lead_name} and noticed you don't seem to have a website yet. "
        "I'm {your_name}, I run {studio_name}, a small local web design studio.\n\n"
        "I build simple, professional one-page websites for local businesses for a flat "
        "${price} — no subscriptions, no hidden fees. If you're interested, just reply to "
        "this email and I'll design a free preview of what your site could look like. "
        "You only pay if you love it.\n\n"
        "Best,\n{your_name}\n{studio_name}\n{mailing_address}\n\n"
        "If you'd rather not hear from me again, reply with the word \"unsubscribe\" "
        "and I won't contact you again."
    ),
    # Behavior.
    # On by default. An app whose whole point is doing the work on its own
    # should not need two switches found and flipped first — being off, and
    # saying nothing about it, is how it sat silent.
    "automation_on_by_default": False,   # flipped true by the migration below
    "autopilot_enabled": True,   # background processing of replies/payments
    "poll_interval_seconds": 60,
    # Automatic prospecting: the agent runs these searches on a schedule and
    # queues what it finds for your approval. It never emails anyone on its own.
    "auto_search_enabled": True,
    "auto_research_enabled": True,    # let the Researcher hunt missing emails
    "research_per_tick": 3,           # how many leads to research each round
    # An address the Researcher found goes straight onto the lead instead of
    # queueing for a second click. It changes nothing about sending: the cold
    # email itself is still read and approved by a person, with that address
    # shown, so a wrong one is caught before anything leaves.
    "auto_accept_emails": True,
    # How wide to cast the net: "none" only takes businesses with no website
    # at all, "broken" adds dead links, parked domains and social-only pages,
    # "weak" adds sites that are http-only or unusable on a phone.
    "lead_quality": "broken",
    # How many uncontacted leads to bank before the crawl rests. It sweeps the
    # map on its own until it gets there — no typing, no buttons.
    "lead_target": 1000,
    "tiles_per_tick": 3,              # spots on the map swept each round
    "lead_floor": 15,                 # go round the map again below this
    "hunt_interval_hours": 6,         # for the older top-up hunt
    "monthly_google_cap": GOOGLE_CALL_CAP,
    "research_model": RESEARCH_MODEL,   # extraction, not reasoning
    "spend_level": DEFAULT_SPEND,       # off | frugal | normal
    "monthly_lookup_cap": None,         # blank means "whatever the dial says"
    "daily_lookup_cap": None,
    "lookup_searches": 2,               # web searches per paid lookup
    "saved_searches": "",         # one search per line
    "search_interval_hours": 12,
    # 20 searches x 3 pages, twice a day, is ~3,600 Google calls a month —
    # inside the 5,000 free ones, and enough ground per run to actually turn
    # something up. The Setup page projects the cost of any other number.
    "searches_per_run": 20,
    "territory_base": "",         # e.g. "Napanoch, NY"
    "territory_miles": 30,
    "daily_send_cap": 20,         # max approved cold emails sent per day
    # Phone access (dashboard reachable from your phone on the same Wi-Fi).
    "phone_access_enabled": False,
    "phone_pin": "",
    # Push notifications to your phone via the free ntfy.sh service.
    "ntfy_enabled": False,
    "ntfy_topic": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update(stored)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if not cfg.get("automation_on_by_default"):
        # Anyone set up before automation was the default has both switches
        # saved as off, which is why nothing appeared to happen. Turn them on
        # once, and record that it's been done so a deliberate "off" sticks.
        cfg["autopilot_enabled"] = True
        cfg["auto_search_enabled"] = True
        cfg["automation_on_by_default"] = True
        try:
            save_config(cfg)
        except OSError:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # holds API keys
    except OSError:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    place_id TEXT UNIQUE,
    name TEXT NOT NULL,
    address TEXT,
    phone TEXT,
    category TEXT,
    email TEXT,
    social_url TEXT,        -- their Facebook/Instagram page, when that's all they have
    last_called_at TEXT,
    stage TEXT NOT NULL DEFAULT 'found',
    do_not_contact INTEGER NOT NULL DEFAULT 0,
    thread_id TEXT,
    last_rfc_id TEXT,          -- most recent RFC Message-ID in the thread (ours or theirs)
    checkout_generation INTEGER NOT NULL DEFAULT 0,
    stage_before_error TEXT,
    site_html TEXT,
    netlify_site_id TEXT,
    netlify_url TEXT,
    stripe_session_id TEXT,
    stripe_session_url TEXT,
    amount_cents INTEGER,
    email_source TEXT,     -- where JARVIS found the address, when he did
    site_status TEXT,      -- what's wrong with their website: see SITE_* above
    site_note TEXT,        -- the detail behind that verdict
    suggested_email TEXT,
    suggested_email_source TEXT,
    suggested_email_note TEXT,
    researched_at TEXT,
    paid_at TEXT,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_uuid TEXT PRIMARY KEY,
    lead_id INTEGER,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,            -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """Thin sqlite wrapper. One connection per thread, WAL mode, atomic claims."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(app_data_dir(), "solo_studio.db")
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing DBs."""
        c = self._conn()
        have = {row["name"] for row in c.execute("PRAGMA table_info(leads)")}
        for col, decl in (("last_called_at", "TEXT"),
                          ("social_url", "TEXT"),
                          ("amount_cents", "INTEGER"),
                          ("email_source", "TEXT"),
                          ("site_status", "TEXT"),
                          ("site_note", "TEXT"),
                          ("suggested_email", "TEXT"),
                          ("suggested_email_source", "TEXT"),
                          ("suggested_email_note", "TEXT"),
                          ("researched_at", "TEXT")):
            if col not in have:
                with c:
                    c.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")

    def revenue_cents(self) -> int:
        row = self._conn().execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS r FROM leads"
            " WHERE paid_at IS NOT NULL").fetchone()
        return int(row["r"] or 0)

    def pending_cents(self) -> int:
        """Value of payment links that are out but not yet paid."""
        row = self._conn().execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS r FROM leads"
            " WHERE stage=? AND paid_at IS NULL",
            (STAGE_PAYMENT_LINK_SENT,)).fetchone()
        return int(row["r"] or 0)

    def event_counts(self) -> dict:
        """How many times each event kind has happened, ever."""
        return {row["kind"]: row["n"] for row in self._conn().execute(
            "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")}

    def sends_today(self) -> int:
        """Cold emails sent since midnight UTC (for the daily cap)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='outreach_sent'"
            " AND created_at >= ?", (today,)).fetchone()
        return int(row["n"] or 0)

    def leads_awaiting_approval(self) -> list[sqlite3.Row]:
        """Found leads with an email, ready for you to approve."""
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND email IS NOT NULL AND email<>'' ORDER BY created_at",
            (STAGE_FOUND,)).fetchall()

    def leads_to_research(self, limit: int = 3) -> list[sqlite3.Row]:
        """Found leads with no email and no suggestion yet, never researched."""
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND (email IS NULL OR email='')"
            " AND (suggested_email IS NULL OR suggested_email='')"
            " AND researched_at IS NULL ORDER BY created_at LIMIT ?",
            (STAGE_FOUND, limit)).fetchall()

    def leads_to_call(self) -> list[sqlite3.Row]:
        """Businesses with a phone number that outreach hasn't reached yet.

        Whoever you haven't tried yet comes first, so the top of the list is
        never someone you rang two minutes ago. Within that, the ones with no
        email address lead — email can't reach them at all, so a call is the
        only way that lead ever goes anywhere. Then longest-since-tried.
        """
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND phone IS NOT NULL AND phone != ''"
            " ORDER BY last_called_at IS NOT NULL ASC,"
            "          (email IS NOT NULL AND email != '') ASC,"
            "          last_called_at ASC, id ASC",
            (STAGE_FOUND,)).fetchall()

    def leads_needing_email(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? AND do_not_contact=0"
            " AND (email IS NULL OR email='') ORDER BY created_at",
            (STAGE_FOUND,)).fetchall()

    def distinct_replied_leads(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM events"
            " WHERE kind='reply_received' AND lead_id IS NOT NULL").fetchone()
        return int(row["n"] or 0)

    # -- leads ------------------------------------------------------------

    def add_lead(self, *, place_id, name, address, phone, category, email=None,
                 social_url=None, site_status=None, site_note=None) -> int | None:
        """Insert a lead; returns new id, or None if this place already exists."""
        c = self._conn()
        try:
            with c:
                cur = c.execute(
                    "INSERT INTO leads (place_id, name, address, phone, category, email,"
                    " social_url, site_status, site_note, stage, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (place_id, name, address, phone, category, email, social_url,
                     site_status, site_note, STAGE_FOUND, _now(), _now()),
                )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def leads_by_stage(self, stage: str) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM leads WHERE stage=? ORDER BY updated_at DESC", (stage,)
        ).fetchall()

    def all_leads(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM leads ORDER BY updated_at DESC").fetchall()

    def find_lead_for_reply(self, thread_id: str | None, from_address: str) -> sqlite3.Row | None:
        """Match an inbound email to a lead: thread id first, then sender address."""
        c = self._conn()
        if thread_id:
            row = c.execute(
                "SELECT * FROM leads WHERE thread_id=?", (thread_id,)).fetchone()
            if row:
                return row
        addr = (from_address or "").strip().lower()
        if not addr:
            return None
        return c.execute(
            "SELECT * FROM leads WHERE lower(email)=? ORDER BY updated_at DESC LIMIT 1",
            (addr,),
        ).fetchone()

    def claim(self, lead_id: int, from_stages: list[str], to_stage: str) -> bool:
        """Atomically move a lead between stages. False means someone else got
        there first (or the lead isn't in an expected stage) — the caller must
        then do nothing. This is the core double-run guard."""
        qmarks = ",".join("?" * len(from_stages))
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE leads SET stage=?, updated_at=? WHERE id=? AND stage IN ({qmarks})",
                [to_stage, _now(), lead_id, *from_stages],
            )
        return cur.rowcount == 1

    def update_lead(self, lead_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE leads SET {cols} WHERE id=?",
                      [*fields.values(), lead_id])

    def bump_attempts(self, lead_id: int) -> int:
        with self._conn() as c:
            c.execute("UPDATE leads SET attempts=attempts+1, updated_at=? WHERE id=?",
                      (_now(), lead_id))
        row = self.get_lead(lead_id)
        return row["attempts"] if row else 0

    # -- processed messages ----------------------------------------------

    def message_seen(self, message_uuid: str) -> bool:
        return self._conn().execute(
            "SELECT 1 FROM processed_messages WHERE message_uuid=?",
            (message_uuid,)).fetchone() is not None

    def mark_message_processed(self, message_uuid: str, lead_id: int | None) -> bool:
        """Record a message as handled. False if it was already recorded
        (another poll got it first)."""
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO processed_messages (message_uuid, lead_id, processed_at)"
                    " VALUES (?,?,?)", (message_uuid, lead_id, _now()))
            return True
        except sqlite3.IntegrityError:
            return False

    # -- events / log ------------------------------------------------------

    def log(self, lead_id: int | None, kind: str, detail: str,
            needs_attention: bool = False) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (lead_id, kind, detail, needs_attention, created_at)"
                " VALUES (?,?,?,?,?)",
                (lead_id, kind, detail, 1 if needs_attention else 0, _now()))

    # -- built-in assistant chat -------------------------------------------

    def chat_history(self, limit: int = 40) -> list[sqlite3.Row]:
        """Oldest-first, capped to the most recent `limit` turns."""
        rows = self._conn().execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed(rows))

    def chat_add(self, role: str, content: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO chat_messages (role, content, created_at)"
                      " VALUES (?,?,?)", (role, content, _now()))

    def chat_clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM chat_messages")

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def attention_events(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM events WHERE needs_attention=1 AND resolved=0 ORDER BY id DESC"
        ).fetchall()

    def resolve_event(self, event_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE events SET resolved=1 WHERE id=?", (event_id,))

    # -- kv ----------------------------------------------------------------

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self._conn().execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def bump_kv(self, key: str, by: int = 1) -> int:
        """Add to a counter and return the new total."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value = CAST(CAST(kv.value AS INTEGER) + ? AS TEXT)",
                (key, str(by), by))
        try:
            return int(self.get_kv(key) or 0)
        except ValueError:
            return 0

    def set_kv(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

WATERMARK_BANNER = (
    '<div id="ss-preview-banner" style="position:fixed;top:0;left:0;right:0;'
    'z-index:2147483647;background:#101418;color:#fff;text-align:center;'
    'padding:10px 16px;font:600 14px/1.4 -apple-system,BlinkMacSystemFont,'
    "'Segoe UI',sans-serif;box-shadow:0 1px 6px rgba(0,0,0,.35)\">"
    'DESIGN PREVIEW — this is a watermarked draft. The final site goes live '
    'once the project is complete.</div>'
)

_WM_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='420' height='300'>"
    "<text x='50%25' y='50%25' text-anchor='middle' fill='rgba(20,20,20,0.10)' "
    "font-family='Helvetica,Arial,sans-serif' font-size='42' font-weight='bold' "
    "transform='rotate(-30 210 150)'>PREVIEW</text></svg>"
)

WATERMARK_OVERLAY = (
    '<div id="ss-preview-overlay" style="position:fixed;inset:0;'
    'z-index:2147483646;pointer-events:none;'
    f'background-image:url(&quot;data:image/svg+xml,{_WM_SVG}&quot;);'
    'background-repeat:repeat"></div>'
)


def inject_watermark(html: str) -> str:
    """Return the watermarked-preview variant of a generated site."""
    marks = WATERMARK_BANNER + WATERMARK_OVERLAY
    m = re.search(r"<body[^>]*>", html, flags=re.IGNORECASE)
    if m:
        return html[: m.end()] + marks + html[m.end():]
    return marks + html


# ---------------------------------------------------------------------------
# External services (everything that touches the network lives here so the
# state machine can be tested against fakes)
# ---------------------------------------------------------------------------

class ServiceError(RuntimeError):
    """A service call failed in a way worth showing the user."""


# The failures that actually happen, and what to do about each one. Services
# report these as JSON a paragraph long; what the user needs is one sentence
# and the button to press. Matched against the error text, first hit wins.
PLAIN_ERRORS = (
    ("credit balance is too low",
     "This Anthropic account has $0 of API credit — the key itself is fine. "
     "Add $5 at console.anthropic.com/settings/billing. A Claude Pro or Max "
     "subscription doesn't count, and each account in the console's switcher "
     "has its own balance."),
    ("invalid x-api-key",
     "Claude didn't accept that key. Copy a fresh one from console.anthropic.com "
     "and paste it on the Setup page."),
    ("authentication_error",
     "Claude didn't accept that key. Copy a fresh one from console.anthropic.com "
     "and paste it on the Setup page."),
    ("rate_limit_error",
     "Claude is being asked for too much at once. It sorts itself out — wait a "
     "minute and try again."),
    ("overloaded_error",
     "Claude is overloaded right now. Nothing is broken; try again in a minute."),
    ("api key not valid",
     "Google didn't accept that key. Check it on the Setup page, and make sure "
     "the key has no website or app restriction on it."),
    ("request_denied",
     "Google refused the search. Usually that means the Places API isn't "
     "switched on for this key yet, or billing isn't enabled on the Google "
     "project."),
    ("billing has not been enabled",
     "Google needs billing switched on for the project before it will search. "
     "Google gives $200 of free use a month, so this normally costs nothing."),
    ("service_disabled",
     "The Places API isn't switched on for this Google project yet. Enable "
     "'Places API (New)' in the Google Cloud console."),
    ("invalid api key provided",
     "Stripe didn't accept that key. Copy it again from the Stripe dashboard — "
     "it starts with sk_."),
    ("failed to resolve",
     "No internet connection, or it dropped mid-request. Check the Wi-Fi and "
     "try again."),
    ("max retries exceeded",
     "Couldn't reach the service — probably the internet connection. Try again "
     "in a moment."),
)


def explain(e, limit: int = 200) -> str:
    """Turn a service failure into a sentence the user can act on.

    Anything unrecognised comes through as-is (trimmed), because a raw message
    is still better than a vague one.
    """
    text = str(e).strip()
    low = text.lower()
    for needle, plain in PLAIN_ERRORS:
        if needle in low:
            return plain
    return text[:limit]


# A Facebook page sitting in Google's "website" slot is not a website. It is
# the clearest signal on the whole listing that this business never got one —
# and it is a warmer lead than a blank, because someone there already tried.
# Same for the rest: a profile on somebody else's platform, or a Google
# Business "site" from the builder Google shut down, whose links mostly 404.
SOCIAL_ONLY_HOSTS = (
    "facebook.com", "fb.me", "fb.com", "instagram.com", "linktr.ee",
    "linktree.com", "yelp.com", "business.site", "sites.google.com",
    "g.page", "tiktok.com", "twitter.com", "x.com", "linkedin.com",
    "nextdoor.com", "wa.me", "menulink.online",
)


def social_platform(url: str) -> str:
    """The platform a 'website' link really is, or '' when it's a real site."""
    if not url:
        return ""
    host = urlsplit(url if "//" in url else "//" + url).netloc.lower()
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    for known in SOCIAL_ONLY_HOSTS:
        if host == known or host.endswith("." + known):
            return known
    return ""


# What can be wrong with a business's website, worst first. Each one is a
# reason to call them that is true and specific — which is the difference
# between a pitch and spam.
SITE_NONE = "none"            # no website at all: the cleanest pitch there is
SITE_DEAD = "dead"            # the link Google has doesn't load
SITE_PARKED = "parked"        # a placeholder or domain-for-sale page
SITE_SOCIAL = "social"        # a Facebook page standing in for a website
SITE_INSECURE = "insecure"    # http only — browsers label it "Not secure"
SITE_NOT_MOBILE = "not-mobile"  # no viewport: unusable on a phone
SITE_OK = "ok"                # a real, working, modern site — leave them alone

SITE_REASON = {
    SITE_NONE: "no website at all",
    SITE_DEAD: "their website doesn't load",
    SITE_PARKED: "their domain is parked — nothing on it",
    SITE_SOCIAL: "only a social page, no website",
    SITE_INSECURE: "their site is http only, so browsers flag it as not secure",
    SITE_NOT_MOBILE: "their site isn't built for phones",
}

# How wide to cast the net, in order. Each level adds to the one before it.
QUALITY_LEVELS = {
    "none": (SITE_NONE,),
    "broken": (SITE_NONE, SITE_SOCIAL, SITE_DEAD, SITE_PARKED),
    "weak": (SITE_NONE, SITE_SOCIAL, SITE_DEAD, SITE_PARKED,
             SITE_INSECURE, SITE_NOT_MOBILE),
}

# Markers of a page that exists but says nothing. Kept narrow on purpose: a
# false positive here means pitching someone who has a perfectly good site.
PARKED_MARKERS = (
    "this domain is for sale", "domain for sale", "buy this domain",
    "parked free, courtesy", "coming soon", "under construction",
    "site is currently unavailable", "future home of", "godaddy.com/domains",
    "this site can't be reached", "default web page",
)
SITE_TIMEOUT = 8
SITE_WORKERS = 12
SITE_MAX_BYTES = 200_000


EMAIL_RE = re.compile(r"[A-Za-z0-9._%%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Addresses that belong to the web plumbing rather than to the business.
EMAIL_JUNK = ("noreply", "no-reply", "donotreply", "sentry.io", "wixpress",
              "example.com", "@2x", "@sentry", "godaddy", "squarespace",
              "wordpress", "yourdomain", "domain.com", "email.com",
              "sentry-next", "@adobe", "core.js", ".png", ".jpg", ".gif",
              ".webp", ".svg", ".css", "@types")


def scrape_email(url: str, timeout: int = SITE_TIMEOUT) -> tuple[str, str]:
    """Read a contact address straight off a page. Returns (email, url).

    Free — an ordinary web request, no model and no search. Worth trying on
    every lead before anything that costs money: a working-but-dated site
    usually has the address right there in the footer, and so do plenty of
    directory listings.
    """
    if not url:
        return "", ""
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoloStudio/1.0)"})
    except requests.RequestException:
        return "", ""
    try:
        if resp.status_code >= 400:
            return "", ""
        try:
            body = resp.raw.read(SITE_MAX_BYTES, decode_content=True) or b""
        except Exception:
            body = resp.content[:SITE_MAX_BYTES]
        text = body.decode("utf-8", "ignore")
    finally:
        resp.close()

    best = ""
    for found in EMAIL_RE.findall(text):
        low = found.lower()
        if any(bad in low for bad in EMAIL_JUNK) or len(found) > 80:
            continue
        # A generic business address beats a named person's.
        if low.split("@")[0] in ("info", "contact", "hello", "office",
                                 "sales", "admin", "mail"):
            return found, resp.url
        best = best or found
    return best, (resp.url if best else "")


def check_website(url: str, timeout: int = SITE_TIMEOUT) -> tuple[str, str]:
    """Look at a business's website and say what, if anything, is wrong with it.

    Costs nothing — this is an ordinary web request, not a billable API call —
    and it is the difference between "20 of them have websites, sorry" and
    "6 of these websites are broken, here they are".
    """
    if not url:
        return SITE_NONE, ""
    platform = social_platform(url)
    if platform:
        return SITE_SOCIAL, platform
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SoloStudio/1.0)"})
    except requests.RequestException as e:
        return SITE_DEAD, type(e).__name__
    try:
        if resp.status_code >= 400:
            return SITE_DEAD, f"HTTP {resp.status_code}"
        try:
            body = resp.raw.read(SITE_MAX_BYTES, decode_content=True) or b""
        except Exception:
            body = resp.content[:SITE_MAX_BYTES]
        text = body.decode("utf-8", "ignore")
        low = text.lower()
        for marker in PARKED_MARKERS:
            if marker in low:
                return SITE_PARKED, marker
        if len(text.strip()) < 500:
            return SITE_PARKED, "almost nothing on the page"
        if not resp.url.lower().startswith("https://"):
            return SITE_INSECURE, resp.url[:120]
        if "name=\"viewport\"" not in low and "name='viewport'" not in low:
            return SITE_NOT_MOBILE, "no viewport tag"
        return SITE_OK, ""
    finally:
        resp.close()


# Things that turn up in a map search and are not small businesses that buy
# websites. A sheriff's office has no website to sell and no owner to sell to.
#
# Two lists, because the two signals are not equally safe. The category comes
# from the map data and can be trusted; a name cannot. "Church Street Auto
# Repair" is a business, and a blunt name test throws it away — so the name
# list holds only phrases that cannot belong to a trading business.
NOT_A_BUSINESS_CATEGORY = (
    "sheriff", "police", "fire department", "fire station", "courthouse",
    "city hall", "town hall", "county clerk", "post office", "dmv",
    "motor vehicles", "library", "school", "university", "college",
    "church", "mosque", "synagogue", "temple", "chapel", "cathedral",
    "place of worship", "hospital", "medical center", "prison", "jail",
    "correctional", "embassy", "consulate", "government", "municipal",
    "township", "national park", "state park", "cemetery", "airport",
    "civic center", "convention center", "city government", "county government",
    "federal", "housing authority", "chamber of commerce", "visitor center",
    "fairground", "courthouse", "public works", "water district",
)
NOT_A_BUSINESS_NAME = (
    # Only phrases that cannot belong to a trading business. "Courthouse
    # Coffee", "Town Hall Tavern" and "The Old Post Office Cafe" are all real
    # businesses, so those words are left to the category test — dropping a
    # genuine lead costs more than letting one town hall through, which the
    # owner can skip in a second.
    "county sheriff", "sheriff s office", "sheriffs office",
    "police department", "police station", "fire department",
    "public library", "high school", "elementary school", "middle school",
    "school district", "board of education", "housing authority",
    "district attorney", "city of", "county of", "town of", "village of",
    "department of", "chamber of commerce",
)


def is_a_business(name: str, category: str = "") -> bool:
    """Is this something a one-person web studio could sell a website to?

    Map searches are full of government offices, schools and churches. They
    are not leads, and putting them in the queue costs the owner the time to
    read past them.
    """
    cat = (category or "").lower()
    if any(word in cat for word in NOT_A_BUSINESS_CATEGORY):
        return False
    low = " " + re.sub(r"\s+", " ",
                       re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())) + " "
    return not any(" " + word + " " in low for word in NOT_A_BUSINESS_NAME)


def _place_row(p: dict) -> dict:
    """One Google place, in the shape a lead is stored in."""
    kind = p.get("primaryTypeDisplayName")
    return {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text", "Unknown"),
        "address": p.get("formattedAddress"),
        "phone": p.get("nationalPhoneNumber"),
        "category": kind.get("text") if isinstance(kind, dict) else kind,
        "social_url": p.get("websiteUri") or None,
    }


def _yelp_row(b: dict) -> dict | None:
    """One Yelp business, in the shape a lead is stored in.

    The url is their Yelp page, not their website — which is exactly why it
    lands in the "only a social/directory page" class.
    """
    name = (b.get("name") or "").strip()
    if not name or b.get("is_closed"):
        return None
    loc = b.get("location") or {}
    address = ", ".join(x for x in (loc.get("display_address") or []) if x)
    cats = ", ".join(c.get("title", "") for c in (b.get("categories") or [])
                     if c.get("title"))
    return {
        "place_id": "yelp:" + str(b.get("id") or name),
        "name": name,
        "address": address or None,
        "phone": b.get("display_phone") or b.get("phone") or None,
        "category": cats or None,
        "social_url": b.get("url") or "https://yelp.com",
    }


def _town_of(address: str) -> str:
    """The town out of a postal address, for when no home town was set.

    "12 Main St, Ellenville, NY 12428" -> "Ellenville, NY". Rough on purpose:
    it only has to be good enough to look up on a map, and anything it gets
    wrong the owner can correct on Setup.
    """
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return ""
    town = parts[-2]
    state = parts[-1].split()[0] if parts[-1].split() else ""
    if len(state) == 2 and state.isalpha():
        return f"{town}, {state.upper()}"
    return town


def _state_of(place: str) -> str:
    """The two-letter state out of "Ellenville, NY", or "" if there isn't one."""
    tail = (place or "").split(",")[-1].strip()
    first = tail.split()[0] if tail.split() else ""
    return first.upper() if len(first) == 2 and first.isalpha() else ""


def _domain_of(url: str) -> str:
    """The bare domain of a real website, or "" for a social page or nothing.

    A Facebook page has no domain of the business's own, so there is nothing
    for a domain-search to look up.
    """
    if not url or social_platform(url):
        return ""
    host = urlsplit(url if "//" in url else "//" + url).netloc.lower()
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _osm_row(el: dict) -> dict | None:
    """One OpenStreetMap element, in the shape a lead is stored in.

    Skips anything without a name — an unnamed point on a map is not a
    business anyone can be written to.
    """
    tags = el.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None
    street = " ".join(x for x in (tags.get("addr:housenumber"),
                                  tags.get("addr:street")) if x)
    town = " ".join(x for x in (tags.get("addr:city"), tags.get("addr:state"),
                                tags.get("addr:postcode")) if x)
    address = ", ".join(x for x in (street, town) if x)
    trade = (tags.get("shop") or tags.get("craft") or tags.get("office")
             or tags.get("amenity") or "")
    # OpenStreetMap sometimes just has the email in it. That is the cheapest
    # address there is: no lookup, no model, no search.
    email = (tags.get("email") or tags.get("contact:email") or "").strip()
    return {
        "place_id": "osm:%s/%s" % (el.get("type"), el.get("id")),
        "name": name,
        "address": address or None,
        "phone": (tags.get("phone") or tags.get("contact:phone")
                  or tags.get("contact:mobile") or None),
        "category": trade.replace("_", " ").title() or None,
        "email": email if EMAIL_RE.fullmatch(email) else None,
        "email_source": ("https://www.openstreetmap.org/%s/%s"
                         % (el.get("type"), el.get("id"))) if email else None,
        "social_url": (tags.get("website") or tags.get("contact:website")
                       or tags.get("contact:facebook") or None),
    }


class SearchResults(list):
    """Leads found, plus what was passed over on the way there.

    A search that comes back empty is not the same as a search that went
    wrong, and neither is the same as a search whose every result already sits
    in the database. Carrying the counts means the app can say which.
    """

    def __init__(self, *a):
        super().__init__(*a)
        self.seen = 0            # businesses Google returned
        self.with_site = 0       # rejected: a real, working, modern website
        self.closed = 0          # rejected: permanently closed
        self.social_only = 0     # kept: only a Facebook/Instagram/Yelp page
        self.by_status = {}      # kept, counted by what's wrong with their site
        self.not_business = 0    # rejected: a school, a church, a sheriff


def describe_search(r: dict) -> str:
    """One sentence saying what a search actually did.

    A search that finds nothing used to say nothing at all, which left the
    only honest question — why? — with no answer anywhere in the app. Every
    outcome here names its own reason.
    """
    seen = r.get("seen", 0)
    if not seen:
        return ("Google returned no businesses at all — check the spelling of "
                "the town, or try a bigger one nearby.")
    added, found = r.get("added", 0), r.get("found", 0)
    bits = [f"looked at {seen}"]
    if r.get("with_site"):
        bits.append(f"{r['with_site']} have a working site")
    if r.get("closed"):
        bits.append(f"{r['closed']} closed down")
    by = r.get("by_status") or {}
    if by:
        worth = ", ".join(
            "%d %s" % (n, SITE_REASON.get(k, k))
            for k, n in sorted(by.items(), key=lambda kv: -kv[1]))
        bits.append("worth pitching: " + worth)
    elif found:
        bits.append(f"{found} worth pitching")
    head = ", ".join(bits)
    if added:
        return f"{head} — {added} new."
    if found:
        return f"{head} — all already in your list."
    return (f"{head}. Every site here loads and works; smaller towns nearby "
            "are where the gaps are.")


# ---------------------------------------------------------------------------
# The watchman
#
# One deterministic pass over the config, the leads and the log that answers
# the only question that matters when you open the app: is anything wrong, and
# what do I do about it. No API calls, no model, no guessing — it runs on every
# background tick and on every page load, so it has to be cheap and it has to
# be the same answer every time.
#
# It reports. It never acts: nothing here sends an email, moves money, or
# touches a lead's stage.
# ---------------------------------------------------------------------------

# The five keys, by the name the owner sees on Setup.
API_KEYS = (("Anthropic (Claude)", "anthropic_api_key"),
            ("Inkbox", "inkbox_api_key"),
            ("Netlify", "netlify_api_key"),
            ("Stripe", "stripe_secret_key"),
            ("Google Places", "google_places_api_key"))

FIX = "fix"            # broken — the pipeline can't do its job until it's sorted
WAITING = "waiting"    # working, but it needs a human decision
WATCH = "watch"        # worth knowing, nothing on fire
LEVEL_ORDER = {FIX: 0, WAITING: 1, WATCH: 2}

# How long a lead may sit in a stage that is supposed to take seconds before
# it counts as stuck rather than busy. Time alone is not enough: a step that
# keeps failing and retrying refreshes the lead every time, so it never looks
# old. Repeated attempts catch that one.
STUCK_MINUTES = 30
STUCK_ATTEMPTS = 2
# A payment link nobody has used, and a preview nobody answered, go stale.
STALE_LINK_DAYS = 7
STALE_PREVIEW_DAYS = 5


def _age_minutes(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


def finding(id, level, title, detail, where="/", cta="") -> dict:
    return {"id": id, "level": level, "title": title, "detail": detail,
            "where": where, "cta": cta}


def checkup(db, cfg, key_fields=API_KEYS, extra=(),
            spent: int = None, cap: int = None) -> list[dict]:
    """Everything currently worth telling the owner, worst first."""
    out = []
    cap = cap if cap is not None else setting_int(
        cfg, "monthly_google_cap", GOOGLE_CALL_CAP)

    # -- can it work at all -------------------------------------------------
    missing = [name for name, field in key_fields if not cfg.get(field)]
    if missing:
        out.append(finding(
            "keys-missing", FIX,
            "%d API key%s still missing" % (len(missing),
                                            "" if len(missing) == 1 else "s"),
            "Nothing runs without " + ", ".join(missing) + ".",
            "/setup", "Open Setup"))

    if not cfg.get("mailing_address"):
        out.append(finding(
            "no-mailing-address", FIX, "No mailing address saved",
            "Cold email has to carry a real postal address by law. Add yours "
            "before any outreach goes out.", "/setup", "Add it"))
    if not cfg.get("your_name"):
        out.append(finding(
            "no-name", WAITING, "Your name isn't filled in",
            "Every email signs off with it.", "/setup", "Add it"))

    if cfg.get("phone_access_enabled") and not cfg.get("phone_pin"):
        out.append(finding(
            "phone-open", FIX, "Phone access is on with no PIN",
            "Anyone on your Wi-Fi can open your dashboard. Set a PIN or turn "
            "phone access off.", "/setup", "Fix it"))

    # -- what the log is complaining about ----------------------------------
    attention = db.attention_events()
    if attention:
        kinds = {}
        for e in attention:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        worst = ", ".join(f"{k.replace('_', ' ')} ({v})"
                          for k, v in sorted(kinds.items(),
                                             key=lambda kv: -kv[1])[:3])
        out.append(finding(
            "attention", FIX,
            "%d thing%s went wrong" % (len(attention),
                                       "" if len(attention) == 1 else "s"),
            worst + ". Each one is on the Activity page with what happened.",
            "/activity", "See what"))

    recent = " ".join((e["detail"] or "") for e in db.recent_events(40))
    if "$0 of API credit" in recent or "credit balance is too low" in recent:
        out.append(finding(
            "credit-empty", FIX, "Claude has no API credit",
            "Every step that writes or designs anything is failing. Add credit "
            "at console.anthropic.com/settings/billing.", "/setup", "Open Setup"))

    # -- leads that are stuck or broken -------------------------------------
    leads = db.all_leads()
    errored = [l for l in leads if l["stage"] == STAGE_ERROR]
    if errored:
        out.append(finding(
            "leads-error", FIX,
            "%d lead%s gave up with an error" % (len(errored),
                                                 "" if len(errored) == 1 else "s"),
            ", ".join(l["name"] for l in errored[:3])
            + ("..." if len(errored) > 3 else "")
            + ". Their last error is on the lead.", "/", "Look"))

    stuck, retrying = [], []
    for l in leads:
        if l["stage"] not in TRANSIENT_STAGES:
            continue
        if (l["attempts"] or 0) >= STUCK_ATTEMPTS:
            retrying.append(l)
        elif _age_minutes(l["updated_at"]) > STUCK_MINUTES:
            stuck.append(l)
    if stuck or retrying:
        n = len(stuck) + len(retrying)
        why = ("failing and retrying" if retrying else
               "sitting there for over %d minutes" % STUCK_MINUTES)
        out.append(finding(
            "leads-stuck", FIX,
            "%d lead%s stuck mid-step" % (n, "" if n == 1 else "s"),
            "These stages take seconds. %s means something is going wrong "
            "quietly — Activity says what."
            % why.capitalize(), "/activity", "See why"))

    # -- waiting on the human ----------------------------------------------
    waiting = db.leads_awaiting_approval()
    if waiting:
        out.append(finding(
            "approve-queue", WAITING,
            "%d cold email%s waiting for you" % (len(waiting),
                                                 "" if len(waiting) == 1 else "s"),
            "Nothing goes out until you read it and press send.",
            "/approve", "Review them"))

    need_email = db.leads_needing_email()
    if need_email:
        out.append(finding(
            "need-email", WAITING,
            "%d lead%s with no email address" % (len(need_email),
                                                 "" if len(need_email) == 1 else "s"),
            "They can't be contacted until one is found. The Researcher can "
            "suggest them, or you can call instead.", "/calls", "Call them"))

    # -- quietly going cold -------------------------------------------------
    stale_links = [l for l in leads if l["stage"] == STAGE_PAYMENT_LINK_SENT
                   and _age_minutes(l["updated_at"]) > STALE_LINK_DAYS * 1440]
    if stale_links:
        out.append(finding(
            "payment-stale", WATCH,
            "%d payment link older than %d days" % (len(stale_links),
                                                    STALE_LINK_DAYS),
            "Sent and never used. Worth a phone call.", "/calls", "Call"))

    stale_previews = [l for l in leads if l["stage"] == STAGE_PREVIEW_SENT
                      and _age_minutes(l["updated_at"]) > STALE_PREVIEW_DAYS * 1440]
    if stale_previews:
        out.append(finding(
            "preview-stale", WATCH,
            "%d preview with no answer in %d days" % (len(stale_previews),
                                                      STALE_PREVIEW_DAYS),
            "They saw a site with their name on it and went quiet.",
            "/calls", "Call"))

    # -- reasons JARVIS might be doing nothing ------------------------------
    # Silence is the worst outcome here. If he isn't working, that is the most
    # important thing on the screen, not something to leave the owner guessing
    # about.
    if not cfg.get("autopilot_enabled"):
        out.append(finding(
            "autopilot-off", FIX, "JARVIS is switched off",
            "He isn't hunting for leads, reading replies or checking payments. "
            "Nothing happens on its own until this is back on.",
            "/setup", "Turn him on"))
    elif not cfg.get("auto_search_enabled"):
        out.append(finding(
            "search-off", FIX, "Lead hunting is switched off",
            "JARVIS is running, but he won't go looking for anyone. No new "
            "leads will appear on their own.", "/setup", "Turn it on"))
    elif spent is not None and spent >= cap:
        out.append(finding(
            "google-spent", FIX, "This month's search budget is used up",
            "%d Google searches used of the %d you allow, so hunting has "
            "stopped until the 1st. Raise the cap in Setup if you want more "
            "(the first 5,000 a month are free)." % (spent, cap),
            "/setup", "Setup"))

    if str(cfg.get("stripe_secret_key", "")).startswith("sk_test"):
        out.append(finding(
            "stripe-test", WATCH, "Stripe is in test mode",
            "Perfect for a practice run — but no real money can be taken.",
            "/setup", "Setup"))

    out.extend(extra)
    out.sort(key=lambda f: LEVEL_ORDER.get(f["level"], 9))
    return out


class Services:
    def __init__(self, config: dict, meter=None):
        self.config = config
        self._inkbox = None
        self._identity = None
        self._anthropic = None
        # Called once per billable Google call. It may refuse, which is the
        # only thing standing between "JARVIS does everything" and a bill.
        self.meter = meter

    # -- Google Places (New) ----------------------------------------------

    def places_search_no_website(self, query: str,
                                 max_results: int = 60) -> SearchResults:
        """Text-search businesses and keep the ones with no website of their own.

        "No website" includes a listing whose only link is a Facebook page or
        the like: that business has nowhere of its own to send a customer,
        which is the entire pitch. Counts of what was passed over come back
        with the results so an empty search can explain itself.
        """
        key = self.config.get("google_places_api_key", "")
        if not key:
            raise ServiceError("Google Places API key is not set (see Setup).")
        url = "https://places.googleapis.com/v1/places:searchText"
        field_mask = ",".join([
            "places.id", "places.displayName", "places.formattedAddress",
            "places.nationalPhoneNumber", "places.websiteUri",
            "places.primaryTypeDisplayName", "places.businessStatus",
            "nextPageToken",
        ])
        headers = {
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": field_mask,
            "Content-Type": "application/json",
        }
        results = SearchResults()
        raw: list[dict] = []
        page_token = None
        while len(results) < max_results:
            body: dict = {"textQuery": query, "pageSize": 20}
            if page_token:
                body["pageToken"] = page_token
            if self.meter:
                self.meter()
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code != 200:
                raise ServiceError(
                    f"Google Places error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            for p in data.get("places", []):
                results.seen += 1
                if p.get("businessStatus") not in (None, "OPERATIONAL"):
                    results.closed += 1
                    continue
                raw.append({
                    "place_id": p.get("id"),
                    "name": (p.get("displayName") or {}).get("text", "Unknown"),
                    "address": p.get("formattedAddress"),
                    "phone": p.get("nationalPhoneNumber"),
                    "category": p.get("primaryTypeDisplayName", {}).get("text")
                    if isinstance(p.get("primaryTypeDisplayName"), dict)
                    else p.get("primaryTypeDisplayName"),
                    "social_url": p.get("websiteUri") or None,
                })
            page_token = data.get("nextPageToken")
            if not page_token or len(raw) >= max_results:
                break

        return self._triage(raw, results, max_results)

    def _triage(self, raw: list[dict], results: "SearchResults",
                max_results: int) -> "SearchResults":
        """Look at every website and keep the businesses worth pitching.

        Every source — Google's text search, Google by distance, OpenStreetMap
        — comes through here, so a lead means the same thing whichever index
        it was found in.
        """
        keep = QUALITY_LEVELS.get(
            self.config.get("lead_quality", "broken"), QUALITY_LEVELS["broken"])
        wanted, dropped = [], 0
        for lead in raw:
            if is_a_business(lead.get("name"), lead.get("category")):
                wanted.append(lead)
            else:
                dropped += 1
        raw = wanted
        results.not_business = dropped
        for lead, (status, note) in zip(raw, self._check_sites(raw)):
            if status not in keep:
                results.with_site += 1
                continue
            lead["site_status"] = status
            lead["site_note"] = note[:200] if note else None
            if status == SITE_SOCIAL:
                results.social_only += 1
            results.by_status[status] = results.by_status.get(status, 0) + 1
            results.append(lead)
        del results[max_results:]
        return results

    # -- Google, by distance rather than fame --------------------------------

    def places_geocode(self, area: str) -> tuple[float, float] | None:
        """Where a town is. One search, and the answer never changes."""
        key = self.config.get("google_places_api_key", "")
        if not key:
            raise ServiceError("Google Places API key is not set (see Setup).")
        if self.meter:
            self.meter()
        resp = requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={"X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "places.location",
                     "Content-Type": "application/json"},
            json={"textQuery": area, "pageSize": 1}, timeout=30)
        if resp.status_code != 200:
            raise ServiceError(
                f"Google Places error {resp.status_code}: {resp.text[:300]}")
        places = resp.json().get("places") or []
        if not places:
            return None
        loc = places[0].get("location") or {}
        if "latitude" not in loc or "longitude" not in loc:
            return None
        return float(loc["latitude"]), float(loc["longitude"])

    def places_nearby(self, lat: float, lng: float, radius: int = 4000,
                      types: list[str] = None,
                      max_results: int = 20) -> SearchResults:
        """The businesses *nearest* a point, rather than the best known ones.

        This is the difference that matters. A text search ranks by prominence,
        which is almost a definition of "has a website"; ranking by distance
        returns the one-van operation on the side street, which is the whole
        market. Twenty per call, no pagination — so sweep with several points
        rather than asking for more.
        """
        key = self.config.get("google_places_api_key", "")
        if not key:
            raise ServiceError("Google Places API key is not set (see Setup).")
        body = {
            "maxResultCount": max(1, min(20, max_results)),
            "rankPreference": "DISTANCE",
            "locationRestriction": {"circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(max(1, min(50000, radius)))}},
        }
        if types:
            body["includedTypes"] = list(types)
        raw = self._nearby_call(body)
        results = SearchResults()
        results.seen = len(raw)
        return self._triage(raw, results, max_results)

    def _nearby_call(self, body: dict) -> list[dict]:
        """One Nearby Search. Retries without the type filter if Google
        rejects a type name — a wrong type would silently return nothing,
        which is worse than a broader search."""
        key = self.config.get("google_places_api_key", "")
        headers = {
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": ",".join([
                "places.id", "places.displayName", "places.formattedAddress",
                "places.nationalPhoneNumber", "places.websiteUri",
                "places.primaryTypeDisplayName", "places.businessStatus"]),
            "Content-Type": "application/json",
        }
        for attempt in (body, {k: v for k, v in body.items()
                               if k != "includedTypes"}):
            if self.meter:
                self.meter()
            resp = requests.post(
                "https://places.googleapis.com/v1/places:searchNearby",
                headers=headers, json=attempt, timeout=30)
            if resp.status_code == 200:
                return [_place_row(p) for p in resp.json().get("places", [])
                        if p.get("businessStatus") in (None, "OPERATIONAL")]
            if resp.status_code != 400 or "includedTypes" not in attempt:
                raise ServiceError(
                    f"Google Places error {resp.status_code}: {resp.text[:300]}")
        return []

    def scrape_email(self, url: str) -> tuple[str, str]:
        """Read an address off a page. Free; no key, no model, no search."""
        return scrape_email(url)

    # -- Yelp, for coverage Google and OSM both miss -------------------------

    YELP_URL = "https://api.yelp.com/v3/businesses/search"
    YELP_MAX = 50

    def yelp_nearby(self, lat: float, lng: float, radius: int = 5000,
                    term: str = None, max_results: int = 50) -> SearchResults:
        """Local businesses from Yelp.

        Worth being straight about what this can and can't do: Yelp's search
        returns a business's Yelp page, never its own website. So Yelp can
        tell us a business exists, with a phone number, but not whether it
        already has a site. Everything from here is therefore treated as
        "only a Yelp page found" — a real lead under the normal setting, and
        a weaker one than Google or OSM, where we checked the actual site.
        """
        key = (self.config.get("yelp_api_key") or "").strip()
        if not key:
            raise ServiceError("No Yelp key set (it's optional — see Setup).")
        params = {"latitude": lat, "longitude": lng,
                  "radius": int(max(1, min(40000, radius))),
                  "limit": max(1, min(self.YELP_MAX, max_results)),
                  "sort_by": "distance"}
        if term:
            params["term"] = term
        try:
            resp = requests.get(self.YELP_URL, params=params, timeout=30,
                                headers={"Authorization": "Bearer " + key})
        except requests.RequestException as e:
            raise ServiceError(f"Couldn't reach Yelp: {e}") from e
        if resp.status_code == 401:
            raise ServiceError("Yelp didn't accept that key. Check it on Setup.")
        if resp.status_code == 429:
            raise ServiceError("Yelp's daily limit is used up — it resets "
                               "tomorrow. Everything else is unaffected.")
        if resp.status_code != 200:
            raise ServiceError(
                f"Yelp error {resp.status_code}: {resp.text[:200]}")
        raw = []
        for b in (resp.json().get("businesses") or []):
            row = _yelp_row(b)
            if row:
                raw.append(row)
        results = SearchResults()
        results.seen = len(raw)
        return self._triage(raw, results, max_results)

    # -- Hunter, for addresses behind a domain we already know ---------------

    def hunter_email(self, domain: str) -> dict:
        """Ask Hunter for a public address at a domain.

        Only useful where a domain is known — which, for this app, means the
        businesses whose site is dead or parked. A business with no website at
        all has no domain to ask about, and those still go to Claude's web
        search.
        """
        key = (self.config.get("hunter_api_key") or "").strip()
        if not key:
            raise ServiceError("No Hunter key set (it's optional — see Setup).")
        try:
            resp = requests.get(
                "https://api.hunter.io/v2/domain-search", timeout=30,
                params={"domain": domain, "api_key": key, "limit": 5})
        except requests.RequestException as e:
            raise ServiceError(f"Couldn't reach Hunter: {e}") from e
        if resp.status_code in (401, 403):
            raise ServiceError("Hunter didn't accept that key. Check it on Setup.")
        if resp.status_code == 429:
            raise ServiceError("Hunter's monthly quota is used up.")
        if resp.status_code != 200:
            raise ServiceError(
                f"Hunter error {resp.status_code}: {resp.text[:200]}")
        data = (resp.json().get("data") or {})
        best = None
        for row in (data.get("emails") or []):
            value = (row.get("value") or "").strip()
            if not value:
                continue
            # Prefer a generic business address over a named person's.
            generic = (row.get("type") or "") == "generic"
            score = (2 if generic else 0) + (1 if row.get("confidence", 0) >= 70 else 0)
            if best is None or score > best[0]:
                best = (score, value, row)
        if not best:
            return {"found": False, "note": f"Hunter had nothing for {domain}."}
        _, email, row = best
        return {"found": True, "email": email,
                "source": f"https://hunter.io (domain search on {domain})",
                "note": "Hunter, confidence %s%%" % row.get("confidence", "?")}

    # -- OpenStreetMap, which costs nothing at all ---------------------------

    # Overpass is a free service run by volunteers. Be a good guest: one
    # request at a time, a real User-Agent, a bounded query, and a cap on how
    # much is asked for.
    # Several mirrors, because a free volunteer service is allowed to be busy.
    OVERPASS_URLS = ("https://overpass-api.de/api/interpreter",
                     "https://overpass.kumi.systems/api/interpreter",
                     "https://overpass.osm.ch/api/interpreter")
    OVERPASS_MAX = 200
    OSM_SELECTORS = (
        'nwr["shop"]["name"]',
        'nwr["craft"]["name"]',
        'nwr["office"~"^(company|estate_agent|insurance|accountant|lawyer|'
        'travel_agent|it|advertising_agency|architect|financial|employment_agency|'
        'moving_company|photographer|surveyor|tax_advisor)$"]["name"]',
        'nwr["amenity"~"^(car_repair|car_wash|veterinary|dentist|doctors|'
        'driving_school|childcare|bar|cafe|pharmacy|fuel)$"]["name"]',
    )

    def osm_nearby(self, lat: float, lng: float, radius: int = 5000,
                   max_results: int = 60) -> SearchResults:
        """Local businesses from OpenStreetMap.

        A completely separate index from Google's, free, no key, and full of
        exactly the small operators this app is looking for — an OSM entry
        very often has a name and a phone number and no website at all.
        """
        parts = "".join(f"  {sel}(around:{int(radius)},{lat},{lng});\n"
                        for sel in self.OSM_SELECTORS)
        query = (f"[out:json][timeout:40];\n(\n{parts});\n"
                 f"out center {self.OVERPASS_MAX};")
        last = "no mirror answered"
        elements = None
        for url in self.OVERPASS_URLS:
            try:
                resp = requests.post(
                    url, data={"data": query}, timeout=60,
                    headers={"User-Agent":
                             "SoloStudio/1.0 (local business finder)"})
            except requests.RequestException as e:
                last = str(e)
                continue
            if resp.status_code in (429, 502, 503, 504):
                last = "busy (HTTP %d)" % resp.status_code
                continue        # a free service is allowed to be busy
            if resp.status_code != 200:
                raise ServiceError(
                    f"OpenStreetMap error {resp.status_code}: {resp.text[:200]}")
            try:
                elements = resp.json().get("elements", [])
            except ValueError as e:
                raise ServiceError(
                    f"OpenStreetMap sent something odd: {e}") from e
            break
        if elements is None:
            raise ServiceError(
                "Couldn't reach OpenStreetMap — it's free and run by "
                "volunteers, so it's sometimes busy. Google searching is "
                "unaffected. (%s)" % last[:120])

        raw = []
        for el in elements:
            row = _osm_row(el)
            if row:
                raw.append(row)
        results = SearchResults()
        results.seen = len(raw)
        return self._triage(raw, results, max_results)

    def _check_sites(self, leads: list[dict]) -> list[tuple[str, str]]:
        """Verdicts for a batch of websites, looked at side by side.

        One at a time this would be minutes. A business with no link at all
        costs nothing and never leaves the process.
        """
        from concurrent.futures import ThreadPoolExecutor
        urls = [(lead.get("social_url") or "") for lead in leads]
        if not any(urls):
            return [(SITE_NONE, "")] * len(urls)
        with ThreadPoolExecutor(max_workers=SITE_WORKERS) as pool:
            return list(pool.map(check_website, urls))

    # -- Inkbox email ------------------------------------------------------

    def _get_identity(self):
        if self._identity is not None:
            return self._identity
        try:
            from inkbox import Inkbox
        except ImportError as e:
            raise ServiceError("The 'inkbox' package is not installed.") from e
        key = self.config.get("inkbox_api_key", "")
        if not key:
            raise ServiceError("Inkbox API key is not set (see Setup).")
        self._inkbox = Inkbox(api_key=key)
        handle = (self.config.get("inkbox_agent_handle") or "").strip()
        if handle:
            self._identity = self._inkbox.get_identity(handle)
        else:
            identities = self._inkbox.list_identities()
            if len(identities) == 1:
                self._identity = self._inkbox.get_identity(identities[0].agent_handle)
            elif not identities:
                raise ServiceError("No Inkbox identities exist on this account.")
            else:
                names = ", ".join(i.agent_handle for i in identities)
                raise ServiceError(
                    f"Multiple Inkbox identities found ({names}); pick one in Setup.")
        if not self._identity.email_address:
            raise ServiceError(
                "The selected Inkbox identity has no email mailbox assigned.")
        return self._identity

    def email_send(self, *, to: str, subject: str, body_text: str,
                   in_reply_to_rfc_id: str | None = None) -> dict:
        """Send an email; returns {thread_id, rfc_id, message_uuid}."""
        identity = self._get_identity()
        msg = identity.send_email(
            to=[to], subject=subject, body_text=body_text,
            in_reply_to_message_id=in_reply_to_rfc_id,
        )
        return {
            "thread_id": str(msg.thread_id) if msg.thread_id else None,
            "rfc_id": msg.message_id,
            "message_uuid": str(msg.id),
        }

    def email_inbound_since(self, since_iso: str | None) -> list[dict]:
        """All inbound emails since a timestamp (oldest first)."""
        from inkbox import MessageDirection
        identity = self._get_identity()
        out = []
        for msg in identity.iter_emails(direction=MessageDirection.INBOUND,
                                        start_datetime=since_iso):
            out.append({
                "message_uuid": str(msg.id),
                "thread_id": str(msg.thread_id) if msg.thread_id else None,
                "rfc_id": msg.message_id,
                "from_address": msg.from_address,
                "subject": msg.subject or "",
                "snippet": msg.snippet or "",
                "created_at": msg.created_at.isoformat(),
            })
        out.reverse()  # API yields newest first; process oldest first
        return out

    def email_fetch_body(self, message_uuid: str) -> str:
        """Full body text of one message. NOTE: for inbound mail Inkbox marks
        the message read server-side when fetched — we keep our own processed
        table and never rely on the unread flag."""
        identity = self._get_identity()
        detail = identity.get_message(message_uuid)
        if detail.body_text:
            return detail.body_text
        if detail.body_html:
            return re.sub(r"<[^>]+>", " ", detail.body_html)
        return ""

    # -- Claude ------------------------------------------------------------

    def _get_anthropic(self):
        if self._anthropic is not None:
            return self._anthropic
        try:
            import anthropic
        except ImportError as e:
            raise ServiceError("The 'anthropic' package is not installed.") from e
        key = self.config.get("anthropic_api_key", "")
        if not key:
            raise ServiceError("Anthropic API key is not set (see Setup).")
        self._anthropic = anthropic.Anthropic(api_key=key)
        return self._anthropic

    def generate_site_html(self, lead: dict) -> str:
        """Ask Claude to design the site. Returns clean (no watermark) HTML."""
        client = self._get_anthropic()
        model = self.config.get("anthropic_model") or "claude-opus-5"
        details = [f"Business name: {lead['name']}"]
        if lead.get("category"):
            details.append(f"Type of business: {lead['category']}")
        if lead.get("address"):
            details.append(f"Address: {lead['address']}")
        if lead.get("phone"):
            details.append(f"Phone: {lead['phone']}")
        # Deliberately the best model available, whatever the spending dial
        # says: this only runs once somebody has asked for a site, it is the
        # thing being sold, and it is the one place quality is worth paying for.
        prompt = (
            "Design a beautiful, modern, single-page website for this local business:\n\n"
            + "\n".join(details) + "\n\n"
            "Requirements:\n"
            "- One complete, self-contained HTML file (all CSS and JS inline).\n"
            "- Professional and tasteful; pick a palette and typography that fit the "
            "business type. Mobile-responsive.\n"
            "- Sections: hero, about/services, and a contact section showing the real "
            "phone number and address above.\n"
            "- Do NOT invent facts (no fake reviews, prices, hours, or team members). "
            "Where such content would normally go, use graceful generic copy.\n"
            "- No external images; use CSS/inline SVG for visuals.\n"
            "- Output ONLY the HTML document, no commentary."
        )
        with client.messages.stream(
            model=model, max_tokens=48000,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            detail = ""
            if getattr(response, "stop_details", None):
                detail = f" ({response.stop_details.explanation})"
            raise ServiceError("Claude declined to generate this site" + detail)
        text = "".join(b.text for b in response.content if b.type == "text")
        html = _extract_html(text)
        if not html:
            raise ServiceError("Claude's response did not contain an HTML document.")
        return html

    def classify_reply(self, lead: dict, stage: str, body: str) -> str:
        """Classify an inbound reply. Returns one of:
        interested | declined | unsubscribe | unclear.

        The prompt is deliberately conservative — anything ambiguous comes back
        as "unclear" for a person to read — which is what makes the small model
        safe here when the dial asks for it.
        """
        client = self._get_anthropic()
        model = thinking_model(self.config)
        context = {
            STAGE_CONTACTED: "We cold-emailed them offering to design a free website preview.",
            STAGE_PREVIEW_SENT: "We sent them a link to a free preview of their website.",
            STAGE_PAYMENT_LINK_SENT: "We sent them a payment link for the website.",
        }.get(stage, "We are corresponding with them about a website.")
        prompt = (
            f"You triage replies for a small web design studio. {context}\n"
            f"The business ({lead['name']}) replied with the email below.\n\n"
            "Classify the reply. Answer with EXACTLY one word:\n"
            "- interested  (they want to proceed / like it / say yes)\n"
            "- declined    (not interested, no thanks)\n"
            "- unsubscribe (stop contacting me, remove me, spam complaint)\n"
            "- unclear     (questions, requests for changes, anything else)\n\n"
            "When in doubt choose unclear — a human will handle it. Never choose "
            "interested unless the reply clearly says to go ahead.\n\n"
            f"--- REPLY ---\n{body[:4000]}\n--- END ---"
        )
        response = client.messages.create(
            model=model, max_tokens=200,   # the answer is one word
            **({} if model.startswith("claude-haiku")
               else {"output_config": {"effort": "low"}}),
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return "unclear"
        text = "".join(b.text for b in response.content if b.type == "text").lower()
        for intent in ("unsubscribe", "interested", "declined", "unclear"):
            if intent in text:
                return intent
        return "unclear"

    ASSISTANT_BRIEF = """You are the built-in helper inside Solo Studio, a Mac \
app that runs a one-person web design business end to end. You are talking to \
its owner, who is not a programmer. Be warm, brief and concrete.

HOW SOLO STUDIO WORKS
Leads move through fixed stages, in order:
  found -> contacted -> building_preview -> preview_sent ->
  sending_payment_link -> payment_link_sent -> paid -> deploying_final -> delivered
Plus not_interested (they said no) and error (something failed; it can be retried).

1. Scout finds local businesses with no website (Google Places).
2. Researcher hunts each one's public email with web search. It only SUGGESTS;
   the owner accepts or rejects every address.
3. Copywriter drafts the cold email. Nothing is ever sent until the owner taps
   "Approve & send" on the Approve page. There is no auto-send.
4. Triage reads replies and judges interest. Anything ambiguous is parked in
   "Needs your attention" rather than guessed at.
5. Designer builds a one-page site; Deployer publishes a WATERMARKED preview to
   Netlify and emails the link.
6. On a second positive reply, Biller emails a Stripe payment link.
7. Delivery ships the clean, watermark-free site ONLY after Stripe confirms the
   payment cleared, and emails the live link.

THE PAYMENT GATE — never suggest working around this. The final site cannot be
delivered unless Stripe itself reports the payment as paid. It is re-checked at
delivery time, and there is no button or setting that skips it. If the owner
asks to send a site before payment, tell them plainly that the app will not do
that, and suggest sending another preview instead.

WHERE THINGS ARE IN THE APP
- Dashboard — every lead and its stage.
- Approve — found businesses waiting for the owner's OK, showing the exact email
  word for word. Also where a missing email address gets pasted in.
- Team — the eight specialists and what each has done.
- Activity — the full log.
- Setup — API keys (with click-by-click directions), business details, the cold
  email template, automatic lead hunting, phone access, and an Advanced section.
- Updates — install a new version, then restart.
- JARVIS — the live stats screen, with the same watch list on it.

THE WATCH LIST
The app checks itself continuously and the snapshot below opens with what it
found, worst first: FIX means something is broken, WAITING means it needs a
decision only the owner can make, WATCH means worth knowing. That list is on
their screen too, so it is the shared starting point. When they ask what is
wrong, or what to do next, answer from it — name the top item, say what it
means in their words, and where to go. Do not invent problems that are not on
it, and do not soften one that is.

WHAT YOU CAN AND CANNOT DO
You can see the owner's live pipeline (below) and answer anything about it, walk
them through setup, explain why a lead is stuck, suggest wording, and do the
arithmetic on their numbers.
You CANNOT act. You cannot send email, approve a lead, create a payment link,
deploy a site, move money, or change any setting — you have no ability to do any
of it. So never say you have done something or will do it later. Instead name
the page and the button: "open Approve and tap Approve & send on Rivera
Plumbing". If something needs a decision only they can make, say so.

STYLE
Short paragraphs, plain words, no jargon or code unless they ask. Two or three
sentences is usually enough. Use their real numbers from the snapshot rather
than speaking generally. If the snapshot does not contain the answer, say what
you do not know instead of inventing a lead, a figure, or a setting."""

    def assistant_reply(self, history: list[dict], snapshot: str) -> str:
        """Answer the owner's question about their own pipeline.

        Advisory only: no tools are wired up, so this cannot act on anything.
        """
        client = self._get_anthropic()
        # Stays on the best model whatever the dial says: you ask this a
        # handful of times a month, deliberately, and a worse answer to "what
        # should I do next" is not worth the fraction of a penny saved.
        model = (self.config.get("anthropic_model") or "").strip() or MAIN_MODEL
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in history if m.get("content")]
        if not messages:
            raise ServiceError("Nothing to answer.")
        with client.messages.stream(
            model=model,
            max_tokens=2000,
            **({} if model.startswith("claude-haiku")
               else {"output_config": {"effort": "low"}}),
            system=[{"type": "text",
                     "text": self.ASSISTANT_BRIEF,
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": snapshot}],
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            return ("I wasn't able to answer that one. Try rephrasing it, or ask "
                    "me something about your leads or setup.")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or "I didn't have anything to add there — try asking again."

    def towns_near(self, base: str, miles: int) -> list[str]:
        """Ask Claude for the real towns within `miles` of `base`.

        This is the bit a person shouldn't have to do by hand — nobody knows
        every hamlet in their county, and typing them one at a time is how the
        automatic search ends up never being switched on.
        """
        client = self._get_anthropic()
        # No web search here, and the small model. A model already knows what
        # towns are near a city, every answer is checked against the map before
        # it is used, and a wrong one costs a geocode rather than a bad lead.
        # This used to be four web searches on the big model.
        model = (self.config.get("research_model") or "").strip() or RESEARCH_MODEL
        prompt = (
            f"List the towns, villages and hamlets within about {miles} miles "
            f"of {base}.\n\n"
            "Rules:\n"
            "- Real inhabited places only, the kind that have local trades in "
            "them and that people use as a postal address. No townships nobody "
            "names.\n"
            "- Around a big city, its separate suburbs and small incorporated "
            "cities count, and are wanted.\n"
            "- SMALLEST first, biggest last. This is for a one-person web "
            "designer looking for businesses that never got a website, and in "
            "a city centre every business already has one. The small places "
            "are the whole point.\n"
            "- Include the starting place itself, but put it last.\n"
            "- At most 25.\n"
            '- Format each as "Town, ST" on its own line. Nothing else — no '
            "numbering, no commentary, no blank lines."
        )
        response = client.messages.create(
            model=model, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise ServiceError("Couldn't work that area out — try a nearby town.")
        text = "".join(b.text for b in response.content if b.type == "text")
        towns, seen = [], set()
        for line in text.splitlines():
            line = line.strip().lstrip("-•*0123456789. ").strip()
            # a town line looks like "Ellenville, NY" and nothing more
            if not re.fullmatch(r"[A-Za-z .'\-]{2,40},\s*[A-Za-z]{2,20}", line):
                continue
            key = line.lower()
            if key not in seen:
                seen.add(key)
                towns.append(line)
        if not towns:
            raise ServiceError(
                f"Couldn't find any towns near {base!r}. Check the spelling — "
                'it wants something like "Napanoch, NY".')
        return towns[:25]

    def research_email(self, lead: dict) -> dict:
        """Look up a business's public contact email using Claude's web search.

        Returns {"found": bool, "email": str, "source": str, "note": str}.
        Only ever *suggests* — a human confirms before anything is emailed.
        """
        client = self._get_anthropic()
        # Deliberately not the big model. This is an extraction job — read a
        # page, copy an address — and Haiku costs a fifth of Opus per token.
        # Web search is billed per search on top, so the cap is low too.
        model = (self.config.get("research_model") or "").strip() or RESEARCH_MODEL
        who = [f"Business name: {lead['name']}"]
        if lead.get("address"):
            who.append(f"Address: {lead['address']}")
        if lead.get("phone"):
            who.append(f"Phone: {lead['phone']}")
        if lead.get("category"):
            who.append(f"Type: {lead['category']}")
        # When Google gave us their Facebook page instead of a website, hand it
        # over: it is usually the one page on the internet with their email.
        if lead.get("social_url"):
            who.append(f"Their only web presence: {lead['social_url']}")
        prompt = (
            "Find the public contact email address for this specific local "
            "business:\n\n" + "\n".join(who) + "\n\n"
            "It has no website of its own, so check places like its Facebook "
            "page, Yelp or Google listing, a directory, or a chamber-of-commerce "
            "page.\n\n"
            "Rules:\n"
            "- Only report an address you actually saw on a page, with the URL.\n"
            "- It must clearly belong to THIS business (match the address or "
            "phone number above), not a similarly named one elsewhere.\n"
            "- Never invent or guess an address, and never construct one from "
            "the business name. If you can't find one, say so.\n"
            "- Prefer a direct business address over a generic contact form.\n\n"
            "Finish your reply with one final line in exactly this format:\n"
            "RESULT: <email> | <url where you saw it> | <short note>\n"
            "or, if you could not find one:\n"
            "RESULT: none | | <short reason>"
        )
        try:
            kwargs = {
                "model": model,
                "max_tokens": 1500,
                "tools": [{"type": _web_search_tool_for(model),
                           "name": "web_search",
                           "max_uses": max(1, int(
                               self.config.get("lookup_searches", 2) or 2))}],
                "messages": [{"role": "user", "content": prompt}],
            }
            # effort is an Opus/Sonnet control; Haiku rejects it.
            if not model.startswith("claude-haiku"):
                kwargs["output_config"] = {"effort": "low"}
            response = client.messages.create(**kwargs)
        except Exception as e:
            raise ServiceError(f"Email research failed: {explain(e)}") from e
        if response.stop_reason == "refusal":
            return {"found": False, "note": "Claude declined this lookup."}
        text = "".join(b.text for b in response.content if b.type == "text")
        line = ""
        for candidate in reversed(text.splitlines()):
            if candidate.strip().upper().startswith("RESULT:"):
                line = candidate.strip()[len("RESULT:"):].strip()
                break
        if not line:
            return {"found": False, "note": "No usable answer from the search."}
        parts = [p.strip() for p in line.split("|")]
        email = parts[0] if parts else ""
        source = parts[1] if len(parts) > 1 else ""
        note = parts[2] if len(parts) > 2 else ""
        if (not email or email.lower() == "none"
                or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email)):
            return {"found": False, "note": note or "No email found online."}
        return {"found": True, "email": email, "source": source, "note": note}

    # -- phone push notifications (ntfy.sh) --------------------------------

    def push_notify(self, title: str, message: str, priority: str = "default",
                    tags: str = "") -> None:
        """Send a push notification to the user's phone via ntfy.sh.
        The topic name acts as the secret — nothing sensitive is included
        beyond lead name + event. Raises ServiceError on failure so the
        Setup test button can report it; pipeline callers swallow errors."""
        if not self.config.get("ntfy_enabled"):
            return
        topic = (self.config.get("ntfy_topic") or "").strip()
        if not topic:
            raise ServiceError("Notifications are enabled but no topic is set.")
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        resp = requests.post(f"https://ntfy.sh/{topic}",
                             data=message.encode("utf-8"),
                             headers=headers, timeout=10)
        if resp.status_code != 200:
            raise ServiceError(f"ntfy.sh error {resp.status_code}: {resp.text[:200]}")

    # -- Netlify -----------------------------------------------------------

    def _netlify_headers(self) -> dict:
        key = self.config.get("netlify_api_key", "")
        if not key:
            raise ServiceError("Netlify API key is not set (see Setup).")
        return {"Authorization": f"Bearer {key}"}

    def netlify_deploy(self, site_id: str | None, html: str,
                       extra_files: dict[str, str] | None = None) -> dict:
        """Deploy a one-page site via zip upload. Creates the site if needed.
        Returns {site_id, url}."""
        headers = self._netlify_headers()
        if site_id is None:
            resp = requests.post("https://api.netlify.com/api/v1/sites",
                                 headers=headers, json={}, timeout=30)
            if resp.status_code not in (200, 201):
                raise ServiceError(
                    f"Netlify site creation failed {resp.status_code}: {resp.text[:300]}")
            site = resp.json()
            site_id = site["id"]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.html", html)
            for name, content in (extra_files or {}).items():
                zf.writestr(name, content)
        buf.seek(0)
        resp = requests.post(
            f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
            headers={**headers, "Content-Type": "application/zip"},
            data=buf.getvalue(), timeout=120,
        )
        if resp.status_code not in (200, 201):
            raise ServiceError(
                f"Netlify deploy failed {resp.status_code}: {resp.text[:300]}")
        deploy = resp.json()
        deploy_id = deploy["id"]
        # Wait for the deploy to finish processing.
        url = deploy.get("ssl_url") or deploy.get("url")
        for _ in range(30):
            r = requests.get(f"https://api.netlify.com/api/v1/deploys/{deploy_id}",
                             headers=headers, timeout=30)
            if r.status_code == 200:
                d = r.json()
                url = d.get("ssl_url") or d.get("url") or url
                if d.get("state") == "ready":
                    break
                if d.get("state") == "error":
                    raise ServiceError("Netlify deploy ended in error state.")
            time.sleep(2)
        if not url:
            raise ServiceError("Netlify deploy finished but returned no URL.")
        return {"site_id": site_id, "url": url.rstrip("/")}

    # -- Stripe ------------------------------------------------------------

    def _stripe_auth(self) -> dict:
        key = self.config.get("stripe_secret_key", "")
        if not key:
            raise ServiceError("Stripe secret key is not set (see Setup).")
        return {"Authorization": f"Bearer {key}"}

    def stripe_create_checkout(self, lead: dict, preview_url: str) -> dict:
        """Create a Checkout Session for the flat site price.
        Returns {session_id, url}."""
        amount_cents = int(round(float(self.config.get("site_price_usd", 500)) * 100))
        currency = self.config.get("currency", "usd")
        data = {
            "mode": "payment",
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][product_data][name]":
                f"Website for {lead['name']}",
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][quantity]": "1",
            "success_url": f"{preview_url}/thanks.html",
            "cancel_url": preview_url,
            "metadata[lead_id]": str(lead["id"]),
        }
        if lead.get("email"):
            data["customer_email"] = lead["email"]
        headers = {
            **self._stripe_auth(),
            # One session per (lead, checkout generation): retries after a network
            # error map to the same session instead of creating a second link.
            "Idempotency-Key":
                f"solo-studio-lead-{lead['id']}-gen-{lead.get('checkout_generation', 0)}",
        }
        resp = requests.post("https://api.stripe.com/v1/checkout/sessions",
                             headers=headers, data=data, timeout=30)
        if resp.status_code != 200:
            try:
                msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise ServiceError(f"Stripe checkout creation failed: {msg}")
        session = resp.json()
        return {"session_id": session["id"], "url": session["url"]}

    def stripe_get_session(self, session_id: str) -> dict:
        """Fetch a Checkout Session's live status straight from Stripe."""
        resp = requests.get(
            f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            headers=self._stripe_auth(), timeout=30)
        if resp.status_code != 200:
            try:
                msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise ServiceError(f"Stripe session lookup failed: {msg}")
        s = resp.json()
        return {
            "status": s.get("status"),                # open | complete | expired
            "payment_status": s.get("payment_status"),  # paid | unpaid | no_payment_required
            "url": s.get("url"),
        }


def _extract_html(text: str) -> str | None:
    """Pull an HTML document out of a model response."""
    m = re.search(r"```(?:html)?\s*(<!doctype.*?|<html.*?)```", text,
                  flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"<!doctype html.*", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(0).strip()
    m = re.search(r"<html.*</html>", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(0).strip()
    return None


THANKS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thank you!</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;
background:#f6f8fa;color:#1a2330}div{text-align:center;padding:2rem}
h1{font-size:2rem}</style></head>
<body><div><h1>Payment received — thank you!</h1>
<p>Your final website is being prepared and you'll get an email with the live link shortly.</p>
</div></body></html>
"""


# ---------------------------------------------------------------------------
# The agent (state machine)
# ---------------------------------------------------------------------------

class Agent:
    """Drives leads through the pipeline. All transitions are claim-guarded."""

    def __init__(self, db: Database, services: Services, config: dict):
        self.db = db
        self.services = services
        self.config = config
        if hasattr(services, "meter"):
            services.meter = self._spend_google_call

    # -- the money tap -------------------------------------------------------

    def google_calls_this_month(self) -> int:
        try:
            return int(self.db.get_kv(_google_meter_key()) or 0)
        except ValueError:
            return 0

    def _spend_google_call(self) -> None:
        """Count a billable Google call, and refuse once the month's cap is hit.

        Google gives 5,000 Text Search calls a month free and charges about
        $32 per thousand after that. Letting JARVIS work continuously is only
        safe if something says no on the owner's behalf, so this does — the
        default cap sits under the free allowance, and it is the same counter
        whether the calls came from a hunt, a saved search, or a button.
        """
        cap = max(0, setting_int(self.config, "monthly_google_cap",
                                 GOOGLE_CALL_CAP))
        if self.google_calls_this_month() >= cap:
            raise ServiceError(
                "That's %d Google searches this month, which is the cap you're "
                "set to (the free allowance is %d). It resets on the 1st, or "
                "raise the cap in Setup — past the free ones Google charges "
                "about $32 per thousand." % (cap, GOOGLE_FREE_CALLS_MONTH))
        self.db.bump_kv(_google_meter_key())

    def _notify(self, title: str, message: str, priority: str = "default",
                tags: str = "") -> None:
        """Best-effort phone push; never breaks the pipeline."""
        fn = getattr(self.services, "push_notify", None)
        if fn is None:
            return
        try:
            fn(title, message, priority=priority, tags=tags)
        except Exception:
            pass

    # -- lead discovery ----------------------------------------------------

    def find_leads(self, query: str) -> dict:
        found = self.services.places_search_no_website(query)
        added = 0
        for p in found:
            if not p.get("place_id"):
                continue
            if self.db.add_lead(**p) is not None:
                added += 1
        r = {"found": len(found), "added": added,
             "seen": getattr(found, "seen", len(found)),
             "with_site": getattr(found, "with_site", 0),
             "closed": getattr(found, "closed", 0),
             "social_only": getattr(found, "social_only", 0),
             "by_status": dict(getattr(found, "by_status", {}) or {})}
        self.db.log(None, "find_leads", f"{query!r}: {describe_search(r)}")
        return r

    # -- outreach ----------------------------------------------------------

    def render_outreach(self, lead) -> dict:
        """The exact subject/body that would be sent — used by the approval
        screen so nothing goes out unseen."""
        cfg = self.config
        fmt = {
            "lead_name": lead["name"],
            "your_name": cfg.get("your_name") or "the owner",
            "studio_name": cfg.get("studio_name") or "Solo Studio",
            "price": fmt_price(cfg.get("site_price_usd", 500)),
            "mailing_address": cfg.get("mailing_address") or "",
        }
        try:
            subject = (cfg.get("outreach_subject", "").format(**fmt)
                       or f"A website for {lead['name']}")
            body = cfg.get("outreach_body", "").format(**fmt)
        except (KeyError, IndexError, ValueError) as e:
            return {"ok": False,
                    "error": f"Your email template has a bad placeholder: {e}. "
                             "Fix it on the Setup page."}
        return {"ok": True, "subject": subject, "body": body}

    def send_outreach(self, lead_id: int) -> dict:
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["do_not_contact"]:
            return {"ok": False, "error": "Lead is flagged do-not-contact."}
        if not lead["email"]:
            return {"ok": False, "error": "Lead has no email address yet."}
        cap = int(self.config.get("daily_send_cap", 20) or 0)
        if cap and self.db.sends_today() >= cap:
            return {"ok": False,
                    "error": f"Daily limit reached ({cap} cold emails today). "
                             "This protects your sending reputation — the rest "
                             "stay queued for tomorrow."}
        rendered = self.render_outreach(lead)
        if not rendered["ok"]:
            return rendered
        # Claim so a double-clicked button can't send two cold emails.
        if not self.db.claim(lead_id, [STAGE_FOUND], STAGE_CONTACTED):
            return {"ok": False, "error": "Lead is not in 'found' stage."}
        try:
            sent = self.services.email_send(to=lead["email"],
                                            subject=rendered["subject"],
                                            body_text=rendered["body"])
        except Exception as e:
            # Roll back so the lead can be retried.
            self.db.claim(lead_id, [STAGE_CONTACTED], STAGE_FOUND)
            self.db.update_lead(lead_id, error=explain(e, 500))
            self.db.log(lead_id, "outreach_failed", explain(e, 500), needs_attention=True)
            return {"ok": False, "error": explain(e)}
        self.db.update_lead(
            lead_id, thread_id=sent["thread_id"],
            last_rfc_id=sent["rfc_id"], error=None)
        self.db.log(lead_id, "outreach_sent", f"Cold email sent to {lead['email']}")
        return {"ok": True}

    # -- inbound replies ---------------------------------------------------

    def process_replies(self) -> dict:
        """Fetch inbound email since the last poll and act on each reply."""
        since = self.db.get_kv("inbound_watermark")
        try:
            inbound = self.services.email_inbound_since(since)
        except Exception as e:
            self.db.log(None, "poll_error",
                        f"Inbound email poll failed: {explain(e, 400)}"[:500])
            return {"ok": False, "error": explain(e)}
        handled = 0
        earliest_skipped = None  # a message we deliberately left for the next poll
        for msg in inbound:
            if self.db.message_seen(msg["message_uuid"]):
                continue
            lead = self.db.find_lead_for_reply(msg["thread_id"], msg["from_address"])
            if lead is None:
                if self.db.mark_message_processed(msg["message_uuid"], None):
                    self.db.log(
                        None, "unmatched_reply",
                        f"Email from {msg['from_address']} ({msg['subject'][:80]!r}) "
                        "doesn't match any lead — handle manually in your inbox.",
                        needs_attention=True)
                    self._notify("Email needs you",
                                 f"From {msg['from_address']}: {msg['subject'][:100]}",
                                 tags="warning")
                continue
            if lead["stage"] in TRANSIENT_STAGES:
                # A step for this lead is mid-flight; leave the message for the
                # next poll rather than racing it.
                if earliest_skipped is None:
                    earliest_skipped = msg["created_at"]
                continue
            # Claim the message itself before acting, so overlapping polls
            # can't process the same reply twice.
            if not self.db.mark_message_processed(msg["message_uuid"], lead["id"]):
                continue
            self._handle_reply(lead, msg)
            handled += 1
        # Advance the watermark, but never past a message we skipped — the
        # processed-message table absorbs the resulting refetch overlap.
        if inbound:
            newest = earliest_skipped or inbound[-1]["created_at"]
            if newest != since:
                self.db.set_kv("inbound_watermark", newest)
        return {"ok": True, "handled": handled}

    def _handle_reply(self, lead: sqlite3.Row, msg: dict) -> None:
        lead_id = lead["id"]
        stage = lead["stage"]
        self.db.log(lead_id, "reply_received",
                    f"Reply from {msg['from_address']}: {msg['snippet'][:120]}")
        self._notify(f"Reply from {lead['name']}", msg["snippet"][:160] or "(no preview)",
                     tags="email")
        try:
            body = self.services.email_fetch_body(msg["message_uuid"]) or msg["snippet"]
        except Exception:
            body = msg["snippet"]

        # Post-payment replies always go to a human.
        if stage in (STAGE_PAID, STAGE_DEPLOYING_FINAL, STAGE_DELIVERED,
                     STAGE_ERROR, STAGE_NOT_INTERESTED, STAGE_FOUND):
            self.db.log(lead_id, "reply_needs_human",
                        f"Reply in stage {stage!r} — reply from your own inbox.",
                        needs_attention=True)
            return

        try:
            intent = self.services.classify_reply(dict(lead), stage, body)
        except Exception as e:
            self.db.log(lead_id, "classify_failed",
                        f"Couldn't classify reply ({explain(e, 300)}) — handle manually.",
                        needs_attention=True)
            return

        if intent == "unsubscribe":
            self.db.update_lead(lead_id, do_not_contact=1)
            self.db.claim(lead_id, [stage], STAGE_NOT_INTERESTED)
            self.db.log(lead_id, "unsubscribed",
                        "Lead asked not to be contacted — flagged do-not-contact.")
            return
        if intent == "declined":
            self.db.claim(lead_id, [stage], STAGE_NOT_INTERESTED)
            self.db.log(lead_id, "declined", "Lead declined.")
            return
        if intent == "unclear":
            self.db.log(lead_id, "reply_unclear",
                        "Reply needs a human answer (question/change request?). "
                        "Reply from your own inbox.", needs_attention=True)
            self._notify(f"{lead['name']} asked something",
                         "Their reply needs a human answer — check your inbox.",
                         tags="warning")
            return

        # intent == interested
        if stage == STAGE_CONTACTED:
            if self.db.claim(lead_id, [STAGE_CONTACTED], STAGE_BUILDING_PREVIEW):
                self.db.update_lead(lead_id, attempts=0,
                                    last_rfc_id=msg["rfc_id"])
                self._advance_preview(lead_id)
        elif stage == STAGE_PREVIEW_SENT:
            if self.db.claim(lead_id, [STAGE_PREVIEW_SENT], STAGE_SENDING_PAYMENT_LINK):
                self.db.update_lead(lead_id, attempts=0,
                                    last_rfc_id=msg["rfc_id"])
                self._advance_payment_link(lead_id)
        elif stage == STAGE_PAYMENT_LINK_SENT:
            self._resend_payment_link(lead_id, msg["rfc_id"])

    # -- preview pipeline (idempotent, resumable) --------------------------

    def _advance_preview(self, lead_id: int) -> None:
        """From stage building_preview: generate HTML -> deploy watermarked
        preview -> email the link -> preview_sent. Each part is skipped if it
        already succeeded, so retries never redo work or double-email."""
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_BUILDING_PREVIEW:
            return
        try:
            html = lead["site_html"]
            if not html:
                html = self.services.generate_site_html(dict(lead))
                self.db.update_lead(lead_id, site_html=html)
                self.db.log(lead_id, "site_generated",
                            f"Claude designed the site ({len(html)} bytes).")
            if not lead["netlify_url"]:
                deployed = self.services.netlify_deploy(
                    lead["netlify_site_id"], inject_watermark(html),
                    extra_files={"thanks.html": THANKS_HTML})
                self.db.update_lead(lead_id, netlify_site_id=deployed["site_id"],
                                    netlify_url=deployed["url"])
                self.db.log(lead_id, "preview_deployed",
                            f"Watermarked preview live at {deployed['url']}")
            lead = self.db.get_lead(lead_id)
            cfg = self.config
            body = (
                f"Great to hear from you!\n\n"
                f"I went ahead and designed a preview of what your website could look "
                f"like:\n\n    {lead['netlify_url']}\n\n"
                f"It's a watermarked draft — if you like it, just reply and I'll send "
                f"over a secure payment link (${fmt_price(cfg.get('site_price_usd', 500))} flat). "
                f"Once that's done the final, watermark-free site goes live and it's "
                f"all yours.\n\nIf you'd like any changes first, tell me what to tweak."
                f"\n\nBest,\n{cfg.get('your_name') or ''}\n{cfg.get('studio_name') or ''}"
            )
            sent = self.services.email_send(
                to=lead["email"], subject=f"Your website preview — {lead['name']}",
                body_text=body, in_reply_to_rfc_id=lead["last_rfc_id"])
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"], error=None,
                                attempts=0)
            self.db.claim(lead_id, [STAGE_BUILDING_PREVIEW], STAGE_PREVIEW_SENT)
            self.db.log(lead_id, "preview_emailed", "Preview link emailed to lead.")
            self._notify(f"Preview sent — {lead['name']}",
                         f"Watermarked preview is live: {lead['netlify_url']}",
                         tags="art")
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            if attempts >= MAX_ATTEMPTS:
                if self.db.claim(lead_id, [STAGE_BUILDING_PREVIEW], STAGE_ERROR):
                    self.db.update_lead(lead_id,
                                        stage_before_error=STAGE_BUILDING_PREVIEW)
                self.db.log(lead_id, "preview_failed",
                            f"Gave up building preview after {attempts} attempts: {explain(e, 300)}",
                            needs_attention=True)
            else:
                self.db.log(lead_id, "preview_retry",
                            f"Preview step failed (attempt {attempts}): {explain(e, 400)}"[:500])

    # -- payment link pipeline --------------------------------------------

    def _advance_payment_link(self, lead_id: int) -> None:
        """From stage sending_payment_link: create ONE checkout session ->
        email the link -> payment_link_sent."""
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_SENDING_PAYMENT_LINK:
            return
        try:
            session_id = lead["stripe_session_id"]
            session_url = lead["stripe_session_url"]
            if not session_id:
                created = self.services.stripe_create_checkout(
                    dict(lead), lead["netlify_url"] or "https://example.com")
                session_id, session_url = created["session_id"], created["url"]
                amount = int(round(float(self.config.get("site_price_usd", 500)) * 100))
                self.db.update_lead(lead_id, stripe_session_id=session_id,
                                    stripe_session_url=session_url,
                                    amount_cents=amount)
                self.db.log(lead_id, "checkout_created",
                            f"Stripe Checkout Session {session_id} created.")
            cfg = self.config
            body = (
                f"Wonderful — glad you like it!\n\n"
                f"Here's your secure payment link for the flat "
                f"${fmt_price(cfg.get('site_price_usd', 500))}:\n\n    {session_url}\n\n"
                f"As soon as the payment goes through, the watermark comes off and "
                f"your final site goes live automatically — you'll get an email with "
                f"the link.\n\nBest,\n{cfg.get('your_name') or ''}\n"
                f"{cfg.get('studio_name') or ''}"
            )
            sent = self.services.email_send(
                to=lead["email"], subject=f"Payment link — website for {lead['name']}",
                body_text=body, in_reply_to_rfc_id=lead["last_rfc_id"])
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"],
                                error=None, attempts=0)
            self.db.claim(lead_id, [STAGE_SENDING_PAYMENT_LINK], STAGE_PAYMENT_LINK_SENT)
            self.db.log(lead_id, "payment_link_emailed", "Payment link emailed.")
            self._notify(f"Payment link sent — {lead['name']}",
                         f"${fmt_price(self.config.get('site_price_usd', 500))} "
                         "checkout link is in their inbox.", tags="link")
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            if attempts >= MAX_ATTEMPTS:
                if self.db.claim(lead_id, [STAGE_SENDING_PAYMENT_LINK], STAGE_ERROR):
                    self.db.update_lead(lead_id,
                                        stage_before_error=STAGE_SENDING_PAYMENT_LINK)
                self.db.log(lead_id, "payment_link_failed",
                            f"Gave up sending payment link after {attempts} attempts: {explain(e, 300)}",
                            needs_attention=True)
            else:
                self.db.log(lead_id, "payment_link_retry",
                            f"Payment-link step failed (attempt {attempts}): {explain(e, 400)}"[:500])

    def _resend_payment_link(self, lead_id: int, reply_rfc_id: str) -> None:
        """A lead replied after getting the link. Re-send the SAME link if the
        session is still open — never create a second one here."""
        lead = self.db.get_lead(lead_id)
        if lead is None or not lead["stripe_session_id"]:
            return
        try:
            session = self.services.stripe_get_session(lead["stripe_session_id"])
        except Exception as e:
            self.db.log(lead_id, "stripe_poll_error", explain(e, 500), needs_attention=True)
            return
        if session["payment_status"] == "paid":
            return  # payment poller will pick it up
        if session["status"] == "expired":
            self.db.log(lead_id, "checkout_expired",
                        "Lead replied but their payment link expired — use "
                        "'Send new payment link' on the lead page.",
                        needs_attention=True)
            return
        self.db.log(lead_id, "reply_after_link",
                    "Lead replied after getting the payment link — re-sent the same "
                    "link; check the reply in your inbox in case it needs a human "
                    "answer.", needs_attention=True)
        try:
            sent = self.services.email_send(
                to=lead["email"],
                subject=f"Payment link — website for {lead['name']}",
                body_text=(
                    "Just in case it got buried, here's your payment link again:\n\n"
                    f"    {lead['stripe_session_url']}\n\n"
                    "Reply here if you have any questions!"),
                in_reply_to_rfc_id=reply_rfc_id)
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"])
        except Exception as e:
            self.db.log(lead_id, "email_failed", explain(e, 500), needs_attention=True)

    def new_payment_link(self, lead_id: int) -> dict:
        """Manual action: replace an EXPIRED session with a fresh one."""
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["stage"] != STAGE_PAYMENT_LINK_SENT:
            return {"ok": False, "error": "Lead is not waiting on a payment link."}
        if lead["stripe_session_id"]:
            try:
                session = self.services.stripe_get_session(lead["stripe_session_id"])
            except Exception as e:
                return {"ok": False, "error": str(e)}
            if session["payment_status"] == "paid":
                return {"ok": False, "error": "This lead already paid — no new link needed."}
            if session["status"] != "expired":
                return {"ok": False,
                        "error": "The existing payment link is still valid; re-send that instead."}
        if not self.db.claim(lead_id, [STAGE_PAYMENT_LINK_SENT], STAGE_SENDING_PAYMENT_LINK):
            return {"ok": False, "error": "Lead changed stage; refresh and retry."}
        self.db.update_lead(lead_id, stripe_session_id=None, stripe_session_url=None,
                            attempts=0,
                            checkout_generation=(lead["checkout_generation"] or 0) + 1)
        self._advance_payment_link(lead_id)
        lead = self.db.get_lead(lead_id)
        return {"ok": lead["stage"] == STAGE_PAYMENT_LINK_SENT}

    # -- payment polling + final delivery ----------------------------------

    def poll_payments(self) -> dict:
        """Check Stripe for every lead waiting on payment. THE payment gate:
        'paid' is only ever set here (or in _resend's early return path via
        this same poller), from Stripe's own payment_status."""
        checked = paid = 0
        for lead in self.db.leads_by_stage(STAGE_PAYMENT_LINK_SENT):
            if not lead["stripe_session_id"]:
                continue
            checked += 1
            try:
                session = self.services.stripe_get_session(lead["stripe_session_id"])
            except Exception as e:
                self.db.log(lead["id"], "stripe_poll_error", explain(e, 500))
                continue
            if session["payment_status"] == "paid":
                if self.db.claim(lead["id"], [STAGE_PAYMENT_LINK_SENT], STAGE_PAID):
                    self.db.update_lead(lead["id"], paid_at=_now(), attempts=0)
                    self.db.log(lead["id"], "payment_confirmed",
                                "Stripe confirmed payment. Deploying final site.")
                    amount = fmt_price((lead["amount_cents"] or 0) / 100)
                    self._notify(f"{lead['name']} PAID ${amount}",
                                 "Stripe confirmed the payment — deploying their "
                                 "final site now.", priority="high", tags="moneybag")
                    paid += 1
            elif session["status"] == "expired":
                self.db.log(lead["id"], "checkout_expired",
                            "Payment link expired unpaid — send a new one from the "
                            "lead page if they're still interested.",
                            needs_attention=True)
        # Drive delivery for everything paid (including retries from crashes).
        for lead in self.db.leads_by_stage(STAGE_PAID):
            if self.db.claim(lead["id"], [STAGE_PAID], STAGE_DEPLOYING_FINAL):
                self._advance_delivery(lead["id"])
        return {"ok": True, "checked": checked, "newly_paid": paid}

    def _advance_delivery(self, lead_id: int) -> None:
        """From stage deploying_final: verify payment AGAIN against Stripe,
        deploy the clean site over the preview, email the live link."""
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_DEPLOYING_FINAL:
            return
        try:
            # Belt-and-braces: re-verify with Stripe at the moment of delivery.
            session = self.services.stripe_get_session(lead["stripe_session_id"])
            if session["payment_status"] != "paid":
                self.db.claim(lead_id, [STAGE_DEPLOYING_FINAL], STAGE_PAYMENT_LINK_SENT)
                self.db.update_lead(lead_id, paid_at=None)
                self.db.log(lead_id, "payment_gate",
                            "Delivery blocked: Stripe no longer reports this session "
                            "as paid.", needs_attention=True)
                return
            if not lead["site_html"]:
                raise ServiceError("No stored site HTML for this lead.")
            deployed = self.services.netlify_deploy(
                lead["netlify_site_id"], lead["site_html"],
                extra_files={"thanks.html": THANKS_HTML})
            self.db.update_lead(lead_id, netlify_site_id=deployed["site_id"],
                                netlify_url=deployed["url"])
            cfg = self.config
            body = (
                f"Payment received — thank you!\n\n"
                f"Your final website is now LIVE (watermark removed):\n\n"
                f"    {deployed['url']}\n\n"
                f"It's all yours. If you ever want changes or a custom domain "
                f"(like www.{re.sub(r'[^a-z0-9]', '', lead['name'].lower())[:20]}.com), "
                f"just reply to this email.\n\n"
                f"Thanks again for your business!\n{cfg.get('your_name') or ''}\n"
                f"{cfg.get('studio_name') or ''}"
            )
            sent = self.services.email_send(
                to=lead["email"], subject=f"Your website is live! — {lead['name']}",
                body_text=body, in_reply_to_rfc_id=lead["last_rfc_id"])
            self.db.update_lead(lead_id, last_rfc_id=sent["rfc_id"],
                                delivered_at=_now(), error=None, attempts=0)
            self.db.claim(lead_id, [STAGE_DEPLOYING_FINAL], STAGE_DELIVERED)
            self.db.log(lead_id, "delivered", f"Final site delivered: {deployed['url']}")
            self._notify(f"Site delivered — {lead['name']}",
                         f"Watermark off, live at {deployed['url']}", tags="tada")
        except Exception as e:
            attempts = self.db.bump_attempts(lead_id)
            self.db.update_lead(lead_id, error=str(e)[:500])
            # NEVER park a paid lead in 'error' silently — roll back to 'paid'
            # so delivery keeps retrying, and flag it loudly.
            self.db.claim(lead_id, [STAGE_DEPLOYING_FINAL], STAGE_PAID)
            self.db.log(lead_id, "delivery_retry",
                        f"Final delivery failed (attempt {attempts}): {explain(e, 300)} — "
                        "they HAVE paid; delivery will retry automatically.",
                        needs_attention=attempts == MAX_ATTEMPTS)

    # -- manual actions (dashboard buttons) --------------------------------

    def set_email(self, lead_id: int, email: str, source: str = None) -> dict:
        """Put an address on a lead. `source` records where it came from when
        JARVIS found it, so the approval screen can show its working."""
        email = (email or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return {"ok": False, "error": "That doesn't look like an email address."}
        self.db.update_lead(lead_id, email=email, email_source=(source or None))
        self.db.log(lead_id, "email_set", f"Email set to {email}")
        return {"ok": True}

    def manual_advance(self, lead_id: int) -> dict:
        """'They're interested' button — same transition the reply classifier
        makes, for when the user judges a reply themselves."""
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["do_not_contact"]:
            return {"ok": False, "error": "Lead is flagged do-not-contact."}
        if lead["stage"] == STAGE_CONTACTED:
            if self.db.claim(lead_id, [STAGE_CONTACTED], STAGE_BUILDING_PREVIEW):
                self.db.update_lead(lead_id, attempts=0)
                self.db.log(lead_id, "manual_advance", "Marked interested by you.")
                self._advance_preview(lead_id)
                return {"ok": True}
        elif lead["stage"] == STAGE_PREVIEW_SENT:
            if self.db.claim(lead_id, [STAGE_PREVIEW_SENT], STAGE_SENDING_PAYMENT_LINK):
                self.db.update_lead(lead_id, attempts=0)
                self.db.log(lead_id, "manual_advance",
                            "Marked ready for payment link by you.")
                self._advance_payment_link(lead_id)
                return {"ok": True}
        return {"ok": False,
                "error": f"Can't advance from stage {lead['stage']!r} — payment and "
                         "delivery always go through Stripe."}

    def manual_not_interested(self, lead_id: int, do_not_contact: bool = False) -> dict:
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if lead["stage"] in (STAGE_PAID, STAGE_DEPLOYING_FINAL, STAGE_DELIVERED):
            return {"ok": False, "error": "Lead already paid — can't mark not interested."}
        self.db.claim(lead_id, [lead["stage"]], STAGE_NOT_INTERESTED)
        fields = {"do_not_contact": 1} if do_not_contact else {}
        if fields:
            self.db.update_lead(lead_id, **fields)
        self.db.log(lead_id, "manual_not_interested", "Marked not interested by you.")
        return {"ok": True}

    def retry_from_error(self, lead_id: int) -> dict:
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["stage"] != STAGE_ERROR:
            return {"ok": False, "error": "Lead is not in the error stage."}
        back_to = lead["stage_before_error"]
        if back_to not in TRANSIENT_STAGES:
            return {"ok": False, "error": "Don't know which step to retry."}
        if not self.db.claim(lead_id, [STAGE_ERROR], back_to):
            return {"ok": False, "error": "Lead changed stage; refresh."}
        self.db.update_lead(lead_id, attempts=0, error=None)
        self.db.log(lead_id, "manual_retry", f"Retrying step {back_to!r}.")
        if back_to == STAGE_BUILDING_PREVIEW:
            self._advance_preview(lead_id)
        elif back_to == STAGE_SENDING_PAYMENT_LINK:
            self._advance_payment_link(lead_id)
        elif back_to == STAGE_DEPLOYING_FINAL:
            self._advance_delivery(lead_id)
        return {"ok": True}

    # -- background tick ---------------------------------------------------

    # -- sweeping one town with everything we have ---------------------------

    def town_centre(self, town: str) -> tuple[float, float] | None:
        """Where a town is: known already, remembered, or looked up — in that
        order, so the common case costs nothing."""
        built_in = city_point(town)
        if built_in:
            return built_in
        key = "geo:" + town.lower().strip()
        cached = self.db.get_kv(key)
        if cached:
            try:
                lat, lng = cached.split(",")
                return float(lat), float(lng)
            except ValueError:
                pass
        point = self.services.places_geocode(town)
        if point:
            self.db.set_kv(key, "%f,%f" % point)
        return point

    def sweep_town(self, town: str, radius: int = 5000) -> dict:
        """Everything we can find in one town, from every index we have.

        Three passes that fail independently, because they fail for different
        reasons: Google by distance (the nearest businesses rather than the
        best known), and OpenStreetMap (free, no key, a different map of the
        world altogether). Either one being down must not stop the other.
        """
        point = None
        try:
            point = self.town_centre(town)
        except Exception as e:
            return {"added": 0, "seen": 0,
                    "notes": [f"couldn't place {town}: {explain(e, 90)}"]}
        if not point:
            return {"added": 0, "seen": 0, "notes": ["no location"]}
        return self.sweep_point(town, point[0], point[1], radius)

    def sweep_point(self, label: str, lat: float, lng: float,
                    radius: int = 2500) -> dict:
        """Every index we have, at one spot on the map."""
        added = seen = 0
        notes = []
        town = label

        sources = [
            ("Google by distance",
             lambda: self.services.places_nearby(lat, lng, radius=radius)),
            ("OpenStreetMap",
             lambda: self.services.osm_nearby(lat, lng, radius=radius)),
        ]
        if (self.config.get("yelp_api_key") or "").strip():
            sources.append(("Yelp",
                            lambda: self.services.yelp_nearby(lat, lng,
                                                              radius=radius)))
        for label, call in sources:
            try:
                found = call()
            except Exception as e:
                notes.append(f"{label}: {explain(e, 90)}")
                self.db.log(None, "sweep_failed", f"{label} in {town}: "
                            f"{explain(e, 200)}"[:400])
                continue
            seen += getattr(found, "seen", len(found))
            new = 0
            for lead in found:
                if not lead.get("place_id"):
                    continue
                if self.db.add_lead(**lead) is not None:
                    new += 1
            added += new
            if new:
                self.db.bump_kv("found:" + town.lower().strip(), new)
            self.db.log(None, "sweep",
                        f"{label} around {town}: {getattr(found, 'seen', 0)} "
                        f"businesses, {len(found)} worth pitching, {new} new.")
        return {"added": added, "seen": seen, "notes": notes}

    # -- crawling the whole map, without being asked -------------------------

    # A tile is one spot on the map with a radius round it. Google returns the
    # 20 nearest businesses to a point and nothing more, so covering a town
    # means several points, not one. ~2.2km apart with a 1.8km radius overlaps
    # slightly, which is what you want — gaps are missed businesses.
    TILE_STEP_DEG = 0.02
    TILE_RADIUS = 1800
    TILE_GRID = 3            # 3x3 points per town

    def _tiles_for(self, town: str, lat: float, lng: float) -> list[list]:
        span = range(-(self.TILE_GRID // 2), self.TILE_GRID // 2 + 1)
        # a degree of longitude shrinks towards the poles; a degree of latitude
        # doesn't, so the east-west step has to be widened to keep tiles square
        shrink = max(0.2, math.cos(math.radians(lat)))
        return [[town, round(lat + i * self.TILE_STEP_DEG, 6),
                 round(lng + j * self.TILE_STEP_DEG / shrink, 6)]
                for i in span for j in span]

    def crawl_cities(self) -> list[str]:
        """Every city to work through, in order.

        Home first, then the towns around it, then the whole country — their
        own state before anywhere else, biggest cities first within each. The
        list is names only; each one is placed on the map when the crawl
        reaches it, so starting the crawl costs nothing.
        """
        cfg = self.config
        home = ((cfg.get("territory_base") or "").strip()
                or _town_of(cfg.get("mailing_address")))
        signature = f"{home}|{cfg.get('territory_miles', 30)}|{len(US_CITIES_BY_STATE)}"
        if self.db.get_kv("crawl_cities_sig") == signature:
            try:
                cached = json.loads(self.db.get_kv("crawl_cities") or "[]")
                if cached:
                    return cached
            except ValueError:
                pass

        order = []
        if home:
            order.append(home)
            try:
                order.extend(self.services.towns_near(
                    home, int(cfg.get("territory_miles", 30) or 30)))
            except Exception as e:
                self.db.log(None, "crawl_note",
                            f"Couldn't list the towns near {home} "
                            f"({explain(e, 130)}) — going straight to the "
                            "city list instead.")
        order.extend(us_cities(_state_of(home)))

        seen, cities = set(), []
        for city in order:
            key = city.lower().strip()
            if key and key not in seen:
                seen.add(key)
                cities.append(city)
        self.db.set_kv("crawl_cities", json.dumps(cities))
        self.db.set_kv("crawl_cities_sig", signature)
        self.db.set_kv("crawl_city", "0")
        self.db.set_kv("crawl_tile", "0")
        self.db.log(None, "crawl_note",
                    f"{len(cities)} cities queued, starting at "
                    f"{cities[0] if cities else 'nowhere'}.")
        return cities

    def _kv_int(self, key: str) -> int:
        try:
            return int(self.db.get_kv(key) or 0)
        except (TypeError, ValueError):
            return 0

    def crawl(self) -> dict:
        """Work across the country on its own, a few spots each round.

        City by city, state by state: place the city on the map once, sweep a
        grid of spots across it, move to the next. Progress lives in the
        database, so it carries on across restarts rather than starting over.
        """
        cfg = self.config
        if not cfg.get("auto_search_enabled"):
            return {"skipped": "automatic hunting off"}

        waiting = len(self.db.leads_by_stage(STAGE_FOUND))
        target = max(1, int(cfg.get("lead_target", 1000) or 1000))
        if waiting >= target:
            return {"skipped": f"{waiting} leads banked, which is the target"}

        cities = self.crawl_cities()
        if not cities:
            return {"skipped": "nowhere to start"}

        city_i = self._kv_int("crawl_city") % len(cities)
        tile_i = self._kv_int("crawl_tile")
        per_tick = max(1, int(cfg.get("tiles_per_tick", 3) or 3))
        added = 0
        swept = 0
        city = cities[city_i]

        for _ in range(per_tick):
            city = cities[city_i]
            tiles = self._tiles_of(city)
            if not tiles:                      # couldn't place it: move along
                city_i = (city_i + 1) % len(cities)
                tile_i = 0
                continue
            if tile_i >= len(tiles):
                city_i = (city_i + 1) % len(cities)
                tile_i = 0
                continue
            town, lat, lng = tiles[tile_i]
            try:
                added += self.sweep_point(town, lat, lng,
                                          radius=self.TILE_RADIUS)["added"]
            except Exception as e:
                self.db.log(None, "crawl_failed", explain(e, 250))
            tile_i += 1
            swept += 1

        self.db.set_kv("crawl_city", str(city_i))
        self.db.set_kv("crawl_tile", str(tile_i))
        if added:
            self.db.log(None, "crawl",
                        f"{city} (city {city_i + 1} of {len(cities)}) — "
                        f"{added} new leads from {swept} spots.")
        return {"added": added, "city": city, "at": city_i + 1,
                "of": len(cities), "swept": swept}

    def _tiles_of(self, city: str) -> list[list]:
        """The grid of spots covering one city, worked out once."""
        key = "tiles:" + city.lower().strip()
        cached = self.db.get_kv(key)
        if cached:
            try:
                return json.loads(cached)
            except ValueError:
                pass
        try:
            point = self.town_centre(city)
        except Exception as e:
            self.db.log(None, "crawl_note",
                        f"Couldn't place {city}: {explain(e, 120)}")
            self.db.set_kv(key, "[]")
            return []
        tiles = self._tiles_for(city, point[0], point[1]) if point else []
        self.db.set_kv(key, json.dumps(tiles))
        return tiles

    # -- the hunt ----------------------------------------------------------

    def hunt(self, area: str, want: int = 8, budget: int = 12) -> dict:
        """Go and find leads in an area without being told what to look for.

        Given nothing but "Los Angeles", work out the towns around it and the
        trades worth trying, then keep searching until enough new leads turn
        up or the budget of searches runs out. Stops the moment it has enough,
        so a good area costs two or three searches, not twelve.

        Why this exists: typing a city into a business search returns the
        city, and the biggest, best-known firms in it — every one of which has
        a website. Nobody would guess that from an empty result, and the
        honest answer ("search smaller places, one trade at a time") is work
        the app should be doing itself.
        """
        area = (area or "").strip()
        if not area:
            return {"ok": False, "added": 0, "summary": "No area given."}

        towns, town_note = [], ""
        try:
            miles = int(self.config.get("territory_miles", 30) or 30)
            towns = self.services.towns_near(area, miles)
        except Exception as e:
            town_note = ("Couldn't work out the towns nearby (%s), so this "
                         "searched %s itself." % (explain(e, 120), area))
        if area not in towns:
            towns.append(area)          # the place they asked for, searched last

        trades = [t.strip() for t in
                  (self.config.get("trades") or "").splitlines() if t.strip()]
        trades = trades or list(DEFAULT_TRADES)

        # Same rotation the saved list uses: sweep the towns, changing trade
        # each pass, so a short hunt covers ground instead of one postcode.
        queries = [f"{trades[(j + k) % len(trades)]} in {towns[j]}"
                   for k in range(len(trades)) for j in range(len(towns))]

        added = seen = ran = 0
        best = []
        swept = set()
        for query in queries[:budget]:
            ran += 1
            where = query.split(" in ", 1)[-1]

            # First time in this town, take everything: the nearest businesses
            # rather than the best-known ones, and OpenStreetMap's own map of
            # the place, which costs nothing at all.
            if where not in swept:
                swept.add(where)
                try:
                    sweep = self.sweep_town(where)
                    added += sweep["added"]
                    seen += sweep["seen"]
                    if sweep["added"]:
                        best.append(f"{sweep['added']} around {where}")
                except Exception as e:
                    self.db.log(None, "hunt_failed",
                                f"sweep of {where}: {explain(e, 200)}"[:400])
                if added >= want:
                    break

            try:
                r = self.find_leads(query)
            except Exception as e:
                self.db.log(None, "hunt_failed",
                            f"{query!r}: {explain(e, 250)}"[:400])
                continue
            added += r["added"]
            seen += r["seen"]
            if r["added"]:
                best.append(f"{r['added']} in {where}")
            if added >= want:
                break

        if added:
            summary = ("Found %d new lead%s — %s. Looked at %d businesses "
                       "across %d search%s."
                       % (added, "" if added == 1 else "s", ", ".join(best[:4]),
                          seen, ran, "" if ran == 1 else "es"))
        elif seen:
            summary = ("No luck: %d businesses across %d searches near %s and "
                       "they all had websites already. Try a different trade, "
                       "or somewhere further out."
                       % (seen, ran, area))
        else:
            summary = ("Google returned nothing at all for %s — check the "
                       "spelling, and include the state." % area)
        if town_note:
            summary += " " + town_note
        self.db.log(None, "hunt", summary)
        if added:
            self._notify("%d new leads found" % added,
                         "JARVIS went hunting near %s." % area, tags="mag")
        return {"ok": True, "added": added, "seen": seen, "searched": ran,
                "towns": len(towns), "summary": summary}

    def run_saved_searches(self, force: bool = False) -> dict:
        """Run the saved searches if they're due, queuing what's found for
        approval. Never emails anyone — discovery only."""
        cfg = self.config
        if not force and not cfg.get("auto_search_enabled"):
            return {"ok": True, "skipped": "auto search off"}
        queries = [q.strip() for q in (cfg.get("saved_searches") or "").splitlines()
                   if q.strip()]
        if not queries:
            return {"ok": True, "skipped": "no saved searches"}
        interval = max(1, int(cfg.get("search_interval_hours", 12) or 12)) * 3600
        last = self.db.get_kv("last_auto_search")
        if not force and last:
            try:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last)).total_seconds()
                if elapsed < interval:
                    return {"ok": True, "skipped": "not due yet"}
            except ValueError:
                pass
        self.db.set_kv("last_auto_search", _now())

        # Google bills per search, and the free allowance is 5,000 calls a
        # month. So a run spends a fixed budget and picks up where it left off
        # next time, working round the list instead of running all of it every
        # time — a hundred saved searches on a 12-hour loop would otherwise be
        # hundreds of dollars a month.
        per_run = max(1, int(cfg.get("searches_per_run", 20) or 20))
        try:
            cursor = int(self.db.get_kv("search_cursor") or 0)
        except (TypeError, ValueError):
            cursor = 0
        cursor %= len(queries)
        batch = [queries[(cursor + i) % len(queries)]
                 for i in range(min(per_run, len(queries)))]
        self.db.set_kv("search_cursor", str((cursor + len(batch)) % len(queries)))

        added = seen = kept = failed = 0
        for query in batch:
            try:
                r = self.find_leads(query)
            except Exception as e:
                failed += 1
                self.db.log(None, "auto_search_failed",
                            f"Search {query!r} failed: {explain(e, 300)}"[:400])
                continue
            added += r.get("added", 0)
            seen += r.get("seen", 0)
            kept += r.get("found", 0)

        waiting = len(self.db.leads_awaiting_approval())
        need_email = len(self.db.leads_needing_email())
        # Always say what happened. A run that finds nothing is the one the
        # user most needs explained, and it used to log nothing at all.
        summary = (f"Searched {len(batch)} of {len(queries)} areas, looked at "
                   f"{seen} businesses — {added} new leads.")
        if failed == len(batch):
            summary = (f"All {failed} searches failed — see the entries above "
                       "for why.")
        elif not added:
            if kept:
                summary += " Everyone without a website here is already in your list."
            elif seen:
                summary += (" Every business Google showed had a website. The "
                            "next run moves on to different areas.")
            else:
                summary += " Google returned nothing for these areas."
        self.db.log(None, "auto_search", summary)
        if added:
            self._notify(f"{added} new leads found",
                         f"{waiting} ready for your approval, {need_email} still "
                         "need an email address.", tags="mag")
        return {"ok": True, "added": added, "seen": seen, "kept": kept,
                "searched": len(batch), "total": len(queries), "failed": failed,
                "summary": summary}

    def research_missing_emails(self, force: bool = False, limit: int = None) -> dict:
        """Researcher: hunt public contact emails for leads that lack one.
        Results are SUGGESTIONS — a human accepts them before any outreach."""
        if not force and not self.config.get("auto_research_enabled"):
            return {"ok": True, "skipped": "researcher off"}
        if limit is None:
            limit = max(1, int(self.config.get("research_per_tick", 3) or 3))
        leads = self.db.leads_to_research(limit)
        found = 0
        for lead in leads:
            try:
                result = self._find_email(dict(lead))
            except Exception as e:
                self.db.log(lead["id"], "research_failed", explain(e, 300))
                self.db.update_lead(lead["id"], researched_at=_now())
                continue
            self.db.update_lead(lead["id"], researched_at=_now())
            if result.get("found"):
                self.db.update_lead(
                    lead["id"], suggested_email=result["email"],
                    suggested_email_source=(result.get("source") or "")[:300],
                    suggested_email_note=(result.get("note") or "")[:300])
                if self.config.get("auto_accept_emails", True):
                    # Straight onto the lead. The cold email to it is still
                    # read and approved by a person, with the address and
                    # where it came from both on screen.
                    self.accept_suggested_email(lead["id"])
                    self.db.log(lead["id"], "email_found",
                                f"Found {result['email']} for {lead['name']} "
                                f"({(result.get('source') or 'no source')[:120]}). "
                                "Check it on the Approve page before sending.")
                else:
                    self.db.log(lead["id"], "email_suggested",
                                f"Researcher found {result['email']} for "
                                f"{lead['name']} — needs your OK.")
                found += 1
            else:
                self.db.log(lead["id"], "email_not_found",
                            f"No email found online for {lead['name']}: "
                            f"{result.get('note', '')}"[:300])
        if found:
            self._notify(f"{found} email address{'' if found == 1 else 'es'} found",
                         "The Researcher turned up contact addresses — check "
                         "them on the Approve page.", tags="mag")
        return {"ok": True, "researched": len(leads), "found": found}

    def _find_email(self, lead: dict) -> dict:
        """An address for one lead, cheapest route first.

        Order matters more than anything else here. Asking a model with web
        search costs roughly a nickel to a dime a lead — a few hundred dollars
        across a full list — and most of those lookups are unnecessary,
        because the address is sitting on a page we already have the link to.
        So: read the page (free), then Hunter if there's a domain and a key,
        and only then spend on a search.
        """
        # 1. Free: read it off whatever page we already know about.
        url = lead.get("social_url")
        if url:
            email, source = self.services.scrape_email(url)
            if email:
                return {"found": True, "email": email, "source": source,
                        "note": "read straight off their page"}

        # 2. Cheap and exact, when there's a domain to ask about.
        domain = _domain_of(url)
        if domain and (self.config.get("hunter_api_key") or "").strip():
            try:
                found = self.services.hunter_email(domain)
                if found.get("found"):
                    return found
            except Exception as e:
                self.db.log(lead.get("id"), "hunter_failed", explain(e, 200))

        # 3. The one that costs real money. Budgeted, and last.
        plan = spend_plan(self.config)
        month_cap = plan["lookups_month"]
        used = self.paid_lookups_this_month()
        if used >= month_cap:
            return {"found": False,
                    "note": "Paid lookups for this month are used up (%d of %d) "
                            "— free ones carry on." % (used, month_cap)}
        # A daily cap as well, or a month's budget goes in the first hour: the
        # researcher runs every tick, and there are 1,440 of those in a day.
        day_cap = setting_int(self.config, "daily_lookup_cap", 0) \
            or plan["lookups_day"]
        today = self.paid_lookups_today()
        if day_cap and today >= day_cap:
            return {"found": False,
                    "note": "That's today's %d paid lookups — it picks up "
                            "again tomorrow, and the free ones carry on."
                            % day_cap}
        self.db.bump_kv(_lookup_meter_key())
        self.db.bump_kv(_lookup_day_key())
        return self.services.research_email(lead)

    def paid_lookups_this_month(self) -> int:
        try:
            return int(self.db.get_kv(_lookup_meter_key()) or 0)
        except ValueError:
            return 0

    def paid_lookups_today(self) -> int:
        try:
            return int(self.db.get_kv(_lookup_day_key()) or 0)
        except ValueError:
            return 0

    def accept_suggested_email(self, lead_id: int) -> dict:
        """Human accepts the Researcher's suggestion for a lead."""
        lead = self.db.get_lead(lead_id)
        if lead is None:
            return {"ok": False, "error": "No such lead."}
        if not lead["suggested_email"]:
            return {"ok": False, "error": "No suggestion to accept."}
        result = self.set_email(lead_id, lead["suggested_email"],
                                source=lead["suggested_email_source"])
        if result.get("ok"):
            self.db.update_lead(lead_id, suggested_email=None,
                                suggested_email_source=None,
                                suggested_email_note=None)
        return result

    def reject_suggested_email(self, lead_id: int) -> dict:
        self.db.update_lead(lead_id, suggested_email=None,
                            suggested_email_source=None,
                            suggested_email_note=None)
        self.db.log(lead_id, "email_rejected", "You rejected the suggested email.")
        return {"ok": True}

    def tick_transients(self) -> None:
        """Resume any lead parked in a transient stage (e.g. after a crash or
        a failed attempt). Each _advance_* step is idempotent."""
        for lead in self.db.leads_by_stage(STAGE_BUILDING_PREVIEW):
            self._advance_preview(lead["id"])
        for lead in self.db.leads_by_stage(STAGE_SENDING_PAYMENT_LINK):
            self._advance_payment_link(lead["id"])
        for lead in self.db.leads_by_stage(STAGE_DEPLOYING_FINAL):
            self._advance_delivery(lead["id"])

    def tick(self) -> None:
        """One background round. Never sends cold outreach — that always waits
        for your approval.

        Every part runs on its own. Two things were wrong with doing it in one
        block: finding leads needs Google and nothing else, yet the whole round
        was skipped when no mailbox was set up; and one step throwing took the
        rest of the round down with it, so a mailbox problem quietly stopped
        the crawling and the researching too.
        """
        mailbox = bool(self.config.get("inkbox_api_key"))
        for name, needed, step in (
                ("reading replies", mailbox, self.process_replies),
                ("checking payments", True, self.poll_payments),
                ("finishing half-done work", True, self.tick_transients),
                ("saved searches", True, self.run_saved_searches),
                ("crawling for leads", True, self.crawl),
                ("looking up addresses", True, self.research_missing_emails)):
            if not needed:
                continue
            try:
                step()
            except Exception as e:
                self.db.log(None, "tick_failed",
                            f"{name}: {explain(e, 250)}"[:400])

    def keep_stocked(self) -> dict:
        """Go and find leads before being asked, when the shelf runs low.

        Hunting costs Google calls, so this is deliberately lazy: only when
        there are few uncontacted leads left, only when a home town is set,
        and never more often than the interval. The meter above is the hard
        stop; this is the polite one.
        """
        cfg = self.config
        if not cfg.get("auto_search_enabled"):
            return {"skipped": "automatic hunting off"}
        area = (cfg.get("territory_base") or "").strip()
        if not area:
            return {"skipped": "no home town set"}

        floor = max(1, int(cfg.get("lead_floor", 15) or 15))
        waiting = len(self.db.leads_by_stage(STAGE_FOUND))
        if waiting >= floor:
            return {"skipped": f"{waiting} leads still waiting"}

        hours = max(1, int(cfg.get("hunt_interval_hours", 6) or 6))
        last = self.db.get_kv("last_hunt")
        if last and _age_minutes(last) < hours * 60:
            return {"skipped": "hunted recently"}
        self.db.set_kv("last_hunt", _now())
        return self.hunt(area, want=max(8, floor - waiting))

    def watch(self) -> list[dict]:
        """Look the whole app over and push for anything newly broken.

        Runs whether or not the pipeline is switched on — a paused app can
        still be misconfigured, and that is exactly when nobody is looking.
        A problem notifies once: it has to clear and come back to notify
        again, so a key you haven't got round to adding doesn't buzz all day.
        """
        found = checkup(self.db, self.config)
        problems = [f for f in found if f["level"] == FIX]
        try:
            known = set(json.loads(self.db.get_kv("watch_notified") or "[]"))
        except (TypeError, ValueError):
            known = set()
        ids = {f["id"] for f in problems}
        fresh = [f for f in problems if f["id"] not in known]
        if fresh:
            more = len(fresh) - 1
            self._notify(fresh[0]["title"],
                         fresh[0]["detail"]
                         + (f" (+{more} more)" if more else ""),
                         priority="high", tags="warning")
        if ids != known:
            self.db.set_kv("watch_notified", json.dumps(sorted(ids)))
        return found
