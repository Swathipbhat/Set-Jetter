"""SetJetter agent loop.

run(inputs) is a generator of trace events for the UI:
  {"type": "status"|"tool_call"|"tool_result"|"final"|"error", ...}

inputs = {"films": [{"title", "weight"} x3], "artist": str, "food": str,
          "days": int, "budget": "" | "$" | "$$" | "$$$"}

LLM mode   (ANTHROPIC_API_KEY set): Claude decides which Qloo tools to call.
Script mode (no key): same Qloo tools, deterministic plan - no LLM needed.
Either way every venue in the final answer is checked against what Qloo
actually returned (grounding check), and the day-by-day itinerary is built
from Qloo results only.
"""
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import qloo

MODEL = os.environ.get("SETJETTER_MODEL", "claude-sonnet-5-5")
API = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
AKEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_STEPS = 18
POOL = ThreadPoolExecutor(max_workers=8)

SYSTEM = """You are SetJetter, a travel agent that picks destinations from cultural taste.
The user gives three favorite films (each with a 0-100 vibe weight), optionally a favorite artist and restaurant,
a trip length in days and a budget. You MUST use the Qloo tools - never rely on memory for places.
Plan (issue independent tool calls together in ONE turn so they run in parallel):
1. resolve_entity for each film (kind=movie), the artist (kind=artist) and the restaurant (kind=restaurant) if given.
2. find_destinations with ALL resolved ids and a `weights` map {id: weight} (use 50 for artist/restaurant).
3. For the top 2 destinations, call find_places for hotel (take 2), cafe (take = days+1) and hotspot (take = 2*days), passing the budget.
   Also call find_fashion once per film (film_ids=[that film's id], the city, take 2, the budget) so the traveller can dress like each film within what they can afford.
4. Reply with ONLY a JSON object (no prose, no code fence):
{"dna": {"summary": str, "traits": [str]},
 "matches": [{"city": str, "match_pct": int, "why": str,
   "hotel": {"name": str, "why": str}, "cafe": {"name": str, "why": str},
   "hotspots": [{"name": str, "why": str}],
   "fashion": [{"name": str, "film": str, "why": str}]}]}
Rules: venue and city names must be copied exactly from tool results. In each "why", tie the place
to specific traits or tags. If a category came back empty, use null. Do not invent anything."""


def _llm(messages):
    r = requests.post(f"{API}/v1/messages", timeout=120,
                      headers={"x-api-key": AKEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": MODEL, "max_tokens": 4000, "system": SYSTEM,
                            "tools": qloo.TOOL_SCHEMAS, "messages": messages})
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic API HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _call(name, args, seen):
    t0 = time.time()
    try:
        res = qloo.TOOLS[name](**args)
    except Exception as e:  # report to the model so it can adapt
        return {"error": str(e)[:300], "_ms": int((time.time() - t0) * 1000)}
    for key in ("places", "destinations", "brands"):
        for item in res.get(key, []) or []:
            seen.add((item.get("name") or "").strip().lower())
    if res.get("name"):
        seen.add(res["name"].strip().lower())
    res["_ms"] = int((time.time() - t0) * 1000)
    return res


def _batch(calls, seen):
    """Run independent tool calls in parallel. calls = [(key, name, args)].
    Yields all tool_call events at once, then each tool_result as it finishes; returns {key: result}."""
    futs = {}
    for key, name, args in calls:
        yield {"type": "tool_call", "name": name, "args": args}
        futs[POOL.submit(_call, name, args, seen)] = (key, name)
    out = {}
    for f in as_completed(futs):
        key, name = futs[f]
        out[key] = f.result()
        yield {"type": "tool_result", "name": name, "result": out[key]}
    return out


def _ground(final, seen):
    """Drop any venue/city the model named that no Qloo call returned."""
    dropped = []

    def ok(n):
        good = bool(n) and n.strip().lower() in seen
        if not good and n:
            dropped.append(n)
        return good

    matches = []
    for m in final.get("matches", []):
        if not ok(m.get("city")):
            continue
        for k in ("hotel", "cafe"):
            if m.get(k) and not ok(m[k].get("name")):
                m[k] = None
        m["hotspots"] = [h for h in (m.get("hotspots") or []) if ok(h.get("name"))]
        m["fashion"] = [x for x in (m.get("fashion") or []) if ok(x.get("name"))]
        matches.append(m)
    final["matches"] = matches
    final["grounding"] = {"removed_ungrounded": dropped}
    return final


def _index(results):
    idx = {}
    for res in results:
        for key in ("places", "destinations", "brands"):
            for it in res.get(key, []) or []:
                idx[(it["name"] or "").strip().lower()] = it
    return idx


def _meta(it):
    return {k: it.get(k) for k in ("id", "image", "address", "affinity", "tags", "lat", "lng", "price_level", "matched_by")}


def _why(p):
    bits = []
    if isinstance(p.get("affinity"), (int, float)):
        bits.append(f"taste affinity {int(round(p['affinity'] * 100))}%")
    tags = [t for t in p.get("tags", []) if t != "fixture"][:3]
    if tags:
        bits.append(", ".join(tags))
    return " · ".join(bits) or "ranked by Qloo for your taste profile"


def _itinerary(city, results, days):
    """Day-by-day plan built only from Qloo venues returned for `city`."""
    def got(cat):
        out = []
        for r in results:
            if r.get("city") == city and r.get("category") == cat:
                out += r.get("places") or []
        return out
    cafes, spots = got("cafe"), got("hotspot")
    n = max(1, min(days, math.ceil(len(spots) / 2))) if spots else (1 if cafes else 0)
    plan = []
    for d in range(n):
        slots = []
        if cafes:
            slots.append(("Morning", "Coffee", cafes[d % len(cafes)]))
        for j, label in ((0, "Afternoon"), (1, "Evening")):
            if 2 * d + j < len(spots):
                slots.append((label, "Hotspot", spots[2 * d + j]))
        plan.append({"day": d + 1, "slots": [
            {"time": t, "kind": k, "name": p["name"], "why": _why(p), "address": p.get("address") or "",
             "lat": p.get("lat"), "lng": p.get("lng"), "image": p.get("image")} for t, k, p in slots]})
    return {"days": plan, "trimmed": n < days}


def _enrich(final, results, days):
    idx = _index(results)
    meta = lambda n: _meta(idx.get((n or "").strip().lower(), {}))
    for m in final["matches"]:
        m["meta"] = meta(m["city"])
        for k in ("hotel", "cafe"):
            if m.get(k):
                m[k]["meta"] = meta(m[k]["name"])
        for h in m["hotspots"]:
            h["meta"] = meta(h["name"])
        for x in m.get("fashion", []):
            x["meta"] = meta(x["name"])
        m["itinerary"] = _itinerary(m["city"], results, days)
    return final


def _signal_card(e, weight):
    return {"name": e["name"], "kind": e.get("kind", "movie"), "year": e.get("year"), "image": e.get("image"),
            "tags": e.get("tags"), "weight": weight}


def _script(inp):
    """Deterministic plan over the same tools; no LLM. Independent calls run in parallel."""
    seen, results, ents, weights = set(), [], [], {}
    wanted = [(f["title"], "movie", f["weight"]) for f in inp["films"]]
    if inp["artist"]:
        wanted.append((inp["artist"], "artist", 50))
    if inp["food"]:
        wanted.append((inp["food"], "restaurant", 50))
    # stage 1: resolve every input at once
    res = yield from _batch([(i, "resolve_entity", {"title": t, "kind": k}) for i, (t, k, _) in enumerate(wanted)], seen)
    for i, (_, _, w) in enumerate(wanted):
        if res[i].get("id"):
            ents.append(res[i])
            weights[res[i]["id"]] = w
    active = [e for e in ents if weights[e["id"]] > 0]
    if not active:
        yield {"type": "error", "message": "None of those inputs were found in Qloo (or all weights are 0)."}
        return
    ids = [e["id"] for e in active]
    names = {e["id"]: e["name"] for e in active}
    # stage 2: destinations (the per-signal queries inside run in parallel too)
    res = yield from _batch([(0, "find_destinations", {"film_ids": ids, "weights": weights})], seen)
    d = res[0]
    if not d.get("destinations"):
        yield {"type": "error", "message": "Qloo returned no destinations. " + (d.get("error") or "The query succeeded but was empty; try different films.")}
        return
    traits = []
    for e in active:
        traits += [t for t in e.get("tags", []) if t not in traits]
    days, budget = inp["days"], inp["budget"] or None
    dests = (d.get("destinations") or [])[:2]
    films = [e for e in active if e.get("kind", "movie") == "movie"][:3]
    # stage 3: every venue + outfit lookup for every city in ONE parallel batch
    calls = []
    for ci, dest in enumerate(dests):
        city = dest["name"]
        for cat, take in (("hotel", 2), ("cafe", max(3, days + 1)), ("hotspot", max(6, 2 * days))):
            calls.append(((ci, cat), "find_places", {"city": city, "film_ids": ids, "category": cat, "take": min(take, 10),
                                                      **({"budget": budget} if budget and cat != "hotspot" else {})}))
        for e in films:
            calls.append(((ci, "fashion", e["id"]), "find_fashion", {"film_ids": [e["id"]], "city": city, "take": 2,
                                                                      **({"budget": budget} if budget else {})}))
    got = yield from _batch(calls, seen)
    results.extend(got[k] for k, _, _ in calls)
    matches = []
    for ci, dest in enumerate(dests):
        city, used, fashion = dest["name"], set(), []
        for e in films:
            for b in got[(ci, "fashion", e["id"])].get("brands", []):
                if b["name"] not in used:
                    used.add(b["name"])
                    fashion.append({"name": b["name"], "film": e["name"], "why": f"Fits the look of {e['name']} · " + _why(b)})
        by = [names[i] for i in dest.get("matched_by", []) if i in names] or [names[i] for i in ids[:3]]
        top = lambda cat, n=1: (got[(ci, cat)].get("places") or [])[:n]
        hotel, cafe = top("hotel"), top("cafe")
        matches.append({
            "city": city, "match_pct": int(round((dest.get("affinity") or 0.5) * 100)),
            "why": f"Pulled in by {', '.join(by)} in Qloo's taste graph.",
            "hotel": {"name": hotel[0]["name"], "why": _why(hotel[0])} if hotel else None,
            "cafe": {"name": cafe[0]["name"], "why": _why(cafe[0])} if cafe else None,
            "hotspots": [{"name": p["name"], "why": _why(p)} for p in top("hotspot", 3)],
            "fashion": fashion})
    results.append(d)
    final = {"dna": {"summary": "Taste tags across your picks: " + ", ".join(traits[:8]) + ".", "traits": traits[:10]},
             "matches": matches}
    yield _final(_enrich(_ground(final, seen), results, days), ents, weights, inp, "scripted")


def _final(data, ents, weights, inp, mode):
    return {"type": "final", "data": data, "mode": mode,
            "inputs": {"days": inp["days"], "budget": inp["budget"]},
            "signals": [_signal_card(e, weights.get(e["id"], 50)) for e in ents]}


def _extract_json(text):
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def normalize(raw):
    """Accept the web form's fields and return clean agent inputs."""
    films = [{"title": (raw.get(f"f{i}") or "").strip()[:80], "weight": _int(raw.get(f"w{i}"), 50, 0, 100)} for i in (1, 2, 3)]
    return {"films": films, "artist": (raw.get("artist") or "").strip()[:80], "food": (raw.get("food") or "").strip()[:80],
            "days": _int(raw.get("days"), 3, 1, 7), "budget": raw.get("budget") if raw.get("budget") in ("$", "$$", "$$$") else ""}


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def run(inp):
    if not all(f["title"] for f in inp["films"]):
        yield {"type": "error", "message": "Please provide three films."}
        return
    src = "fixture (offline sample data)" if qloo.FIXTURES else "Qloo live"
    yield {"type": "status", "message": f"Qloo source: {src}. Planner: {'Claude' if AKEY else 'using QLOO api'}."}
    if not AKEY:
        yield from _script(inp)
        return
    seen, results, ents, weights = set(), [], [], {}
    brief = (f"Films (vibe weight): " + "; ".join(f"{f['title']} ({f['weight']})" for f in inp["films"]) +
             (f". Favorite artist: {inp['artist']}" if inp["artist"] else "") +
             (f". Favorite restaurant: {inp['food']}" if inp["food"] else "") +
             f". Trip: {inp['days']} days. Budget: {inp['budget'] or 'any'}.")
    messages = [{"role": "user", "content": brief}]
    try:
        for _ in range(MAX_STEPS):
            resp = _llm(messages)
            messages.append({"role": "assistant", "content": resp["content"]})
            calls = [b for b in resp["content"] if b["type"] == "tool_use"]
            for b in resp["content"]:
                if b["type"] == "text" and calls and b["text"].strip():
                    yield {"type": "status", "message": b["text"].strip()}
            if not calls:
                text = "".join(b.get("text", "") for b in resp["content"])
                final = _enrich(_ground(_extract_json(text), seen), results, inp["days"])
                for e in ents:  # map the user's slider weights onto resolved entities
                    weights.setdefault(e["id"], 50)
                yield _final(final, ents, weights, inp, "claude")
                return
            res = yield from _batch([(b["id"], b["name"], b["input"]) for b in calls], seen)
            out = []
            for b in calls:
                r = res[b["id"]]
                results.append(r)
                if b["name"] in ("resolve_entity", "resolve_movie") and r.get("id"):
                    ents.append(r)
                    for f in inp["films"]:
                        if f["title"].lower() == (b["input"].get("title") or "").lower():
                            weights[r["id"]] = f["weight"]
                out.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(r)[:12000]})
            messages.append({"role": "user", "content": out})
        yield {"type": "error", "message": "Agent exceeded step limit."}
    except Exception as e:
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
