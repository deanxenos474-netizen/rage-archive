"""Daily update: fetch new and upcoming rage playlists from abc.net.au, then rebuild the site data.

Run by .github/workflows/update.yml. Standard library only.
- Playlists that have aired are added to data/playlists.json.gz and counted as plays.
- Playlists posted ahead of time are kept in "upcoming" and re-checked every run until they air.
"""
import datetime, html, json, re, sys, time, urllib.error, urllib.request

import sitebuild

UA = {"User-Agent": "rage-archive-site-updater (+https://github.com)"}
LIST_API = ("https://www.abc.net.au/core-next/api/collection/rage/playlist?collectionId=13642802"
            "&rootMetaCollectionId=104200304&metaInMeta=false&offset={off}&size=50")
PAGE = "https://www.abc.net.au/rage/playlist/x/{id}"
ND = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
TIME = re.compile(r"^\s*\d{1,2}[:.]\d{2}\s*(am|pm)?\s*$", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
ROUTINE = {"new music show", "best new music videos", "new music videos", "new music", "vault", "charts", "chart",
           "rage goes retro", "new releases", "new release", "music videos", "playlist", "new music video",
           "best new music", "new videos", "best new videos", "new release music videos", "new releases music videos"}
CONNECT = {"ft.", "ft", "feat.", "feat", "featuring", "and", "x", "&", "vs", "vs.", "with", "+", "-"}


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return e.code, url, ""
            time.sleep(3 + 5 * i)
        except Exception:
            time.sleep(3 + 5 * i)
    return 0, url, ""


def clean(s):
    s = html.unescape(s or "").replace("�", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---- reading an ABC playlist page (same rules as the original download) ----
def lines_from_descriptor(node):
    out, cur = [], []

    def flush():
        nonlocal cur
        if any(t.strip() for _, t in cur):
            out.append(cur)
        cur = []

    def walk(n, tag):
        if n.get("type") == "text":
            cur.append((tag, n.get("content", "")))
            return
        key = n.get("key", "")
        if key == "br":
            flush(); return
        block = key in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "ul", "ol", "@@standfirst")
        if block:
            flush()
        t = key if key in ("strong", "em", "b", "i") else tag
        for c in n.get("children", []) or []:
            walk(c, t)
        if block:
            flush()

    walk(node, "")
    flush()
    return out


def split_caps(full):
    label = ""
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", full)
    if m:
        full, label = m.group(1), m.group(2)
    toks = full.split()
    k = 0
    while k < len(toks) and (toks[k].lower() in CONNECT or not re.search(r"[a-z]", toks[k])):
        k += 1
    if 0 < k < len(toks):
        return " ".join(toks[:k]), " ".join(toks[k:]), clean(label)
    return None


def parse_line(segs):
    artist = clean(" ".join(t for g, t in segs if g in ("strong", "b")))
    title = clean(" ".join(t for g, t in segs if g in ("em", "i")))
    rest = clean(" ".join(t for g, t in segs if g not in ("strong", "b", "em", "i")))
    if artist and title:
        m = re.search(r"\(([^()]*)\)\s*$", rest)
        return artist, title, (clean(m.group(1)) if m else "")
    if artist and rest and not title:
        m = re.match(r"^(.+?)\s*\(([^()]*)\)\s*$", rest)
        if m:
            return artist, clean(m.group(1)).strip("-– "), clean(m.group(2))
        return artist, rest.strip("-– "), ""
    full = clean(" ".join(t for _, t in segs))
    caps = split_caps(full)
    if caps:
        return caps
    m = re.match(r"^(.+?)\s+[-–]\s+(.+?)\s*\(([^()]*)\)\s*$", full)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def find_doc(o):
    if isinstance(o, dict):
        if "datelinePrepared" in o and isinstance(o.get("text"), dict) and "descriptor" in o["text"]:
            return o
        vals = o.values()
    elif isinstance(o, list):
        vals = o
    else:
        return None
    for v in vals:
        r = find_doc(v)
        if r is not None:
            return r
    return None


def air_date(title, pub):
    s = (title or "").lower()
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", s) or \
        re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", s)
    if not m:
        return pub
    a, b, y = m.groups()
    day, mon = (int(a), MONTHS[b]) if a.isdigit() else (int(b), MONTHS[a])
    p = datetime.date.fromisoformat(pub)
    year = int(y) if y else p.year
    try:
        d = datetime.date(year, mon, day)
    except ValueError:
        return pub
    if not y and (d - p).days < -180:
        d = d.replace(year=year + 1)
    return d.isoformat() if abs((d - p).days) <= 21 else pub


def special_info(raw):
    s = re.sub(r"\s+", " ", (raw or "").replace("\xa0", " ")).strip(" ,.-")
    if not s or re.fullmatch(r"(?i)\d{1,2}[:.]\d{2}\s*(am|pm)?", s):
        return None
    if re.match(r"(?i)^(friday|saturday|sunday|monday|tuesday|wednesday|thursday)\b.*\b(19|20)\d\d\b", s) and len(s) < 60:
        return None
    s = re.sub(r"(?i)\s*(and|&|\+)\s*(the\s+)?(best\s+)?new\s+music(\s+videos?)?\s*$", "", s)

    def routine(x):
        x = re.sub(r"^the\s+", "", re.sub(r"[^a-z0-9 ]", "", x.lower()).strip())
        return x in ROUTINE or x == "rage goes retro"

    keep = [x.strip() for x in s.split(",") if x.strip() and not routine(x)]
    if not keep:
        return None
    text = ", ".join(keep)
    g = re.search(r"(?i)^(.*?)\s+guest[- ]?program", text)
    if g or re.search(r"(?i)guest[- ]?program", text):
        return ["Guest programmer", text, g.group(1).strip() if g else ""]
    return ["Special", text, ""]


def fetch_playlist(pid):
    status, final, page = get(PAGE.format(id=pid))
    m = ND.search(page) if page else None
    if not m or "/rage/playlist/" not in final or "test-" in final:
        return None
    art = find_doc(json.loads(m.group(1)))
    if not art:
        return None
    title = re.sub(r"\s*-\s*ABC Rage$", "", art.get("title") or "")
    pubraw = (art.get("datelinePrepared") or {}).get("publishedDate")
    if not pubraw:
        return None
    pub = (datetime.datetime.fromisoformat(pubraw.replace("Z", "+00:00")) + datetime.timedelta(hours=10)).date().isoformat()
    rows = []
    for segs in lines_from_descriptor(art["text"]["descriptor"]):
        r = parse_line(segs)
        if r and not TIME.match(r[0]):
            rows.append(list(r))
    return {"date": air_date(title, pub), "title": title, "url": final, "src": "abc", "abc_id": pid,
            "sp": special_info(art.get("synopsis")), "rows": rows}


def latest_ids():
    ids = []
    for off in range(0, 250, 50):
        status, _, body = get(LIST_API.format(off=off))
        if not body:
            break
        found = [int(x) for x in re.findall(r"/rage/playlist/[a-z0-9-]+/(\d+)", body)]
        if not found:
            break
        ids += [i for i in found if i not in ids]
    return ids


def main():
    d = sitebuild.load()
    today = sitebuild.today_au()
    seen = set(d.get("abc_seen", []))
    upcoming = {p["abc_id"]: p for p in d.get("upcoming", [])}
    ids = latest_ids()
    if not ids:
        print("could not read the ABC playlist list; nothing changed")
        return 0
    todo = [i for i in ids if i not in seen] + [i for i in upcoming if i not in ids]
    sigs = {(p["date"], json.dumps(p["rows"])) for p in d["playlists"]}
    added, waiting = 0, 0
    for pid in todo:
        p = fetch_playlist(pid)
        time.sleep(0.5)
        if p is None or not p["rows"]:
            if p is not None and p["date"] > today:      # posted early but still empty: check again tomorrow
                continue
            seen.add(pid); upcoming.pop(pid, None)
            continue
        if p["date"] > today:
            upcoming[pid] = p; waiting += 1
            continue
        upcoming.pop(pid, None); seen.add(pid)
        sig = (p["date"], json.dumps(p["rows"]))
        if sig not in sigs:
            d["playlists"].append(p); sigs.add(sig); added += 1
    d["abc_seen"] = sorted(seen)
    d["upcoming"] = sorted(upcoming.values(), key=lambda p: (p["date"], p["title"]))
    sitebuild.save(d)
    meta = sitebuild.build(d)
    print(f"checked {len(todo)} playlist pages: {added} new aired, {waiting} upcoming; "
          f"{meta['songs']} songs, {meta['plays']} plays, {meta['playlists']} broadcasts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
