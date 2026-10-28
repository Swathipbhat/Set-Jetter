"""Thin Qloo client + the tools the SetJetter agent can call.

Live mode talks to the Qloo hackathon server. Fixture mode (QLOO_FIXTURES=1)
returns canned, clearly-flagged sample data so the agent loop can be developed
and tested without network access. Every tool result carries `source`
("qloo-live" or "fixture") so nothing fixture-based is ever passed off as real.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = os.environ.get("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
KEY = os.environ.get("QLOO_API_KEY", "")
FIXTURES = os.environ.get("QLOO_FIXTURES") == "1"


class QlooError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.client_error = status in (400, 404, 422)  # request shape rejected: retrying the same shape is pointless


_SESSION = requests.Session()  # keep-alive: reuse TCP/TLS connections across calls
_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16))
_CACHE, _TTL = {}, 900          # successful GETs are cached for 15 min (repeat searches / share links are instant)
_PLAN = {}                      # learned: first request variant that works, per call type
_POOL = ThreadPoolExecutor(max_workers=6)
TIMEOUT = (float(os.environ.get("QLOO_CONNECT_TIMEOUT", 4)), float(os.environ.get("QLOO_READ_TIMEOUT", 15)))


def _get(path, params):
    if not KEY:
        raise QlooError("QLOO_API_KEY is not set")
    ck = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        r = _SESSION.get(BASE + path, params=params, headers={"X-Api-Key": KEY}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise QlooError(f"Qloo {path} -> {type(e).__name__}")
    if r.status_code >= 400:
        raise QlooError(f"Qloo {path} -> HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    data = r.json()
    _CACHE[ck] = (time.time(), data)
    return data


def _entities(payload):
    """Qloo responses nest entities slightly differently across endpoints."""
    if isinstance(payload, list):
        return payload
    res = payload.get("results", payload)
    if isinstance(res, list):
        return res
    return res.get("entities", []) if isinstance(res, dict) else []


def _insights(params):
    return _get("/v2/insights", params)


def _try(plan_key, variants):
    """Try request variants in order (most specific first) until one returns entities.

    Qloo rejecting a variant (HTTP 400/404/422) is remembered in _PLAN, so later calls of the same
    kind start from the first variant the server accepts instead of re-failing every time.
    Returns (data, index, notes)."""
    start, notes = _PLAN.get(plan_key, 0), []
    for idx in range(start, len(variants)):
        label, params = variants[idx]
        try:
            data = _insights(params)
        except QlooError as e:
            notes.append(f"{label}: {str(e)[:70]}")
            if e.client_error:
                _PLAN[plan_key] = max(_PLAN.get(plan_key, 0), idx + 1)
            continue
        if _entities(data):
            return data, idx, notes
    return None, None, notes


def _coords(e):
    """Find lat/lng wherever this entity keeps them (shape varies by endpoint)."""
    props = e.get("properties") or {}
    for src in (e.get("location"), props.get("geocode"), props.get("location"), props):
        if isinstance(src, dict):
            lat = src.get("lat", src.get("latitude"))
            lng = src.get("lng", src.get("lon", src.get("longitude")))
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                return lat, lng
    return None, None


def _slim(e):
    props = e.get("properties") or {}
    geo = props.get("geocode") or {}
    img = props.get("image") or {}
    lat, lng = _coords(e)
    return {
        "id": e.get("entity_id") or e.get("id"),
        "name": e.get("name"),
        "type": e.get("type") or (e.get("subtype") or ""),
        "year": props.get("release_year"),
        "description": (props.get("description") or "")[:240],
        "address": props.get("address") or geo.get("formatted_address") or "",
        "city": geo.get("city") or "",
        "country": geo.get("country_code") or geo.get("country") or "",
        "image": img.get("url") if isinstance(img, dict) else None,
        "tags": [t.get("name") for t in (e.get("tags") or []) if t.get("name")][:12],
        "tag_ids": [t.get("id") for t in (e.get("tags") or []) if t.get("id")][:12],
        "affinity": (e.get("query") or {}).get("affinity"),
        "rating": props.get("business_rating"),
        "price_level": props.get("price_level"),
        "lat": lat, "lng": lng,
    }


KINDS = {"movie": "urn:entity:movie", "artist": "urn:entity:artist", "restaurant": "urn:entity:place"}


# ---------------------------------------------------------------- tools

def resolve_entity(title, kind="movie"):
    """Resolve a film / artist / restaurant name to a Qloo entity (+ taste tags)."""
    kind = kind if kind in KINDS else "movie"
    if FIXTURES:
        from fixtures import entity
        return entity(title, kind)
    data = _get("/search", {"query": title, "types": KINDS[kind], "take": 3})
    ents = [_slim(e) for e in _entities(data)]
    if not ents:
        return {"source": "qloo-live", "kind": kind, "error": f"No Qloo match for '{title}'"}
    best = ents[0]
    best["alternatives"] = [{"id": e["id"], "name": e["name"], "year": e["year"]} for e in ents[1:]]
    best.update(source="qloo-live", kind=kind)
    return best


def resolve_movie(title):
    return resolve_entity(title, "movie")


def _destinations_once(ids, take):
    if FIXTURES:
        from fixtures import destinations
        return destinations(ids)["destinations"]
    data = _insights({
        "filter.type": "urn:entity:destination",
        "signal.interests.entities": ",".join(ids),
        "take": take})
    return [_slim(e) for e in _entities(data)]


def find_destinations(film_ids, weights=None, take=6):
    """Cities whose taste profile aligns with the signals (cross-domain affinity).

    `weights` ({entity_id: 0-100}) is the user's vibe dial. With weights we query each
    signal on its own and combine the results client-side (weighted affinity), and report
    which signals pulled each city (`matched_by`). Weight 0 drops a signal.
    """
    ids = [i for i in film_ids if (weights or {}).get(i, 50) > 0]
    if not ids:
        return {"source": "fixture" if FIXTURES else "qloo-live", "destinations": [], "error": "All signals have weight 0"}
    if not weights or len(ids) == 1:
        out = _destinations_once(ids, take)
        return {"source": "fixture" if FIXTURES else "qloo-live", "destinations": out, "by_signal": {}}
    total = float(sum(weights.get(i, 50) for i in ids))
    score, info, by_signal = {}, {}, {}
    for i, got in zip(ids, _POOL.map(lambda x: _destinations_once([x], 12), ids)):
        by_signal[i] = [d["name"] for d in got]
        for rank, d in enumerate(got):
            aff = d.get("affinity")
            aff = aff if isinstance(aff, (int, float)) else 1.0 / (rank + 1)
            score[d["name"]] = score.get(d["name"], 0.0) + aff * weights.get(i, 50) / total
            info.setdefault(d["name"], d)
    ranked = sorted(score, key=score.get, reverse=True)[:take]
    out = [{**info[n], "affinity": round(min(score[n], 1.0), 3),
            "matched_by": [i for i in ids if n in by_signal[i]]} for n in ranked]
    return {"source": "fixture" if FIXTURES else "qloo-live", "destinations": out, "by_signal": by_signal}


# category -> (Qloo tag filter attempt, keyword heuristics for fallback classification)
CATEGORIES = {
    "hotel": ("urn:tag:category:place:hotel", ("hotel", "inn", "lodging", "boutique", "ryokan", "hostel")),
    "cafe": ("urn:tag:category:place:cafe", ("cafe", "café", "coffee", "bakery", "tea")),
    "hotspot": (None, ("bar", "restaurant", "gallery", "museum", "club", "park", "temple", "market", "theater", "theatre", "landmark", "library")),
}
BUDGET = {"$": (1, 1), "$$": (2, 2), "$$$": (3, 4)}  # Qloo price_level 1-4


def _classify(p):
    blob = " ".join([p["name"] or "", *p["tags"]]).lower()
    for cat, (_, kws) in CATEGORIES.items():
        if any(k in blob for k in kws):
            return cat
    return "hotspot"


def find_places(city, film_ids, category="hotspot", take=6, budget=None):
    """Real venues in `city` ranked by taste affinity. budget: "$" | "$$" | "$$$" (hotels/cafes)."""
    category = category if category in CATEGORIES else "hotspot"
    if FIXTURES:
        from fixtures import places
        return places(city, category, take)
    base = {"filter.type": "urn:entity:place", "filter.location.query": city,
            "signal.interests.entities": ",".join(film_ids)}
    tag = CATEGORIES[category][0]
    price = {}
    if budget in BUDGET and category in ("hotel", "cafe"):
        price = {"filter.price_level.min": BUDGET[budget][0], "filter.price_level.max": BUDGET[budget][1]}
    variants, seen_v = [], set()
    for label, with_tag, with_price in (("tag+budget", True, True), ("budget, no tag", False, True), ("no tag, no budget", False, False)):
        if (with_tag and not tag) or (with_price and not price):
            continue
        p = {**base, **(price if with_price else {}), **({"filter.tags": tag} if with_tag else {}),
             "take": take if with_tag else take * 3}
        key = tuple(sorted((k, str(v)) for k, v in p.items()))
        if key not in seen_v:
            seen_v.add(key)
            variants.append((label, p))
    data, idx, notes = _try(("places", category, bool(price)), variants)
    used_tag = idx is not None and "tag" in variants[idx][0] and "no tag" not in variants[idx][0]
    if price and idx is not None and "budget" not in variants[idx][0].replace("no budget", ""):
        notes.append("budget filter unavailable; ignored")
    ents = [_slim(e) for e in _entities(data or {})]
    if not used_tag:
        ents = [e for e in ents if _classify(e) == category] or ents
    return {"source": "qloo-live", "city": city, "category": category, "places": ents[:take],
            **({"note": "; ".join(notes)} if notes else {})}


FASHION_WORDS = ("fashion", "clothing", "apparel", "footwear", "accessor", "jewel", "streetwear", "luxury", "denim", "sportswear")


def find_fashion(film_ids, city=None, take=5, budget=None):
    """Fashion brands that fit the taste signals (pass one film id to 'dress like' that film).
    `budget` ("$"/"$$"/"$$$") keeps brands the traveller can afford; dropped with a note if Qloo rejects it."""
    if FIXTURES:
        from fixtures import fashion
        return fashion(city, take, budget)
    base = {"filter.type": "urn:entity:brand", "signal.interests.entities": ",".join(film_ids)}
    price = {"filter.price_level.min": BUDGET[budget][0], "filter.price_level.max": BUDGET[budget][1]} if budget in BUDGET else {}
    tagp = {"filter.tags": "urn:tag:genre:brand:fashion"}
    locp = {"signal.location.query": city} if city else {}
    plans = [("tag+city+budget", tagp, locp, price), ("tag+budget", tagp, {}, price), ("city+budget", {}, locp, price),
             ("budget only", {}, {}, price), ("tag+city", tagp, locp, {}), ("tag only", tagp, {}, {}), ("taste only", {}, {}, {})]
    variants, seen_v = [], set()
    for label, t, l, p in plans:
        params = {**base, **t, **l, **p, "take": take if t else take * 4}
        key = tuple(sorted((k, str(v)) for k, v in params.items()))
        if key not in seen_v:
            seen_v.add(key)
            variants.append((label, params))
    data, idx, notes = _try(("fashion", bool(price), bool(city)), variants)
    used = variants[idx][1] if idx is not None else {}
    if price and "filter.price_level.min" not in used:
        notes.append("budget filter unavailable; ignored")
    ents = [_slim(e) for e in _entities(data or {})]
    if "filter.tags" not in used:  # no server-side fashion filter: keep brands that look like fashion, else all
        ents = [e for e in ents if any(w in " ".join(e["tags"] + [e["name"] or ""]).lower() for w in FASHION_WORDS)] or ents
    return {"source": "qloo-live", "city": city, "brands": ents[:take], **({"note": "; ".join(notes)} if notes else {})}


TOOLS = {"resolve_entity": resolve_entity, "resolve_movie": resolve_movie,
         "find_destinations": find_destinations, "find_places": find_places,
         "find_fashion": find_fashion}

TOOL_SCHEMAS = [
    {"name": "resolve_entity",
     "description": "Look up a film, artist or restaurant in Qloo's taste graph. Returns its entity id and taste tags (its 'DNA'). Call once per input.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"}, "kind": {"type": "string", "enum": ["movie", "artist", "restaurant"]}},
         "required": ["title", "kind"]}},
    {"name": "find_destinations",
     "description": "Given Qloo entity ids, return the cities whose cultural taste profile best matches them. Pass `weights` ({id: 0-100}) to honour the user's vibe dial.",
     "input_schema": {"type": "object", "properties": {
         "film_ids": {"type": "array", "items": {"type": "string"}},
         "weights": {"type": "object", "additionalProperties": {"type": "number"}}},
         "required": ["film_ids"]}},
    {"name": "find_places",
     "description": "Given a city and entity ids, return real venues in that city ranked by taste affinity. category: hotel | cafe | hotspot. Use take >= 2 x trip days for hotspots.",
     "input_schema": {"type": "object", "properties": {
         "city": {"type": "string"},
         "film_ids": {"type": "array", "items": {"type": "string"}},
         "category": {"type": "string", "enum": ["hotel", "cafe", "hotspot"]},
         "take": {"type": "integer"},
         "budget": {"type": "string", "enum": ["$", "$$", "$$$"]}},
         "required": ["city", "film_ids", "category"]}},
    {"name": "find_fashion",
     "description": "Fashion brands matching the taste signals, optionally relevant in a city. Call once per film (film_ids=[that film's id]) to suggest how to dress like that film, within the budget.",
     "input_schema": {"type": "object", "properties": {
         "film_ids": {"type": "array", "items": {"type": "string"}}, "city": {"type": "string"},
         "take": {"type": "integer"}, "budget": {"type": "string", "enum": ["$", "$$", "$$$"]}},
         "required": ["film_ids"]}},
]
