"""Rebuild songs.json and broadcasts.json from data/playlists.json.gz.

Standard library only, so it runs on GitHub Actions without installing anything.
Song ids are a hash of the normalised artist + title, so covers, videos and merges stay attached across rebuilds.
"""
import collections, datetime, gzip, hashlib, json, os, re, unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "playlists.json.gz")

FEAT = re.compile(r"\s+(?:ft\.?|feat\.?|featuring|with)\s+", re.I)
CONN = re.compile(r"\b(ft\.?|feat\.?|featuring|and|vs\.?|x|with|presents|pres\.?)\b")
# non-song content: interviews, segments, promos etc. (not songs that merely contain these words)
MISC = re.compile(r"(?i)\binterview|\bdocumentary\b|\bmontage\b|\bsegment\b|\bpromo\b|\bnews footage\b|"
                  r"\bident\b|\bpress conference\b|\bpremiere of\b")
SHOW = re.compile(r"(?i)^(countdown|gtk|flashez|the meldrum tapes|molly meldrum)"
                  r"(\s*$|\s*[-,(]|\s+(revolution|promo|special|prog|countdown|on countdown|part)\b)")
SHOW_T = re.compile(r"(?i)^(on )?countdown( revolution)?\s*[,(]|\bcountdown episode\b|\bthe making of\b")


def fold(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(s):
    s = fold(s).lower()
    s = FEAT.sub(" feat ", " " + s + " ")
    s = s.replace("&", " and ").replace("+", " and ")
    s = re.sub(r"[‘’'`\"“”]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"^\s*the\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(s):
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s)
    s = re.sub(r"(?i)\s*[\(\[]\s*(censored|clean|explicit|edited|radio edit)( version| edit)?\s*[\)\]]", "", s)
    return norm(s)


def song_id(a, t):
    return hashlib.sha1("\x1f".join((norm(a), norm_title(t))).encode("utf-8")).hexdigest()[:12]


def is_caps(a):
    core = CONN.sub("", a)
    return core == core.upper()


def clean_row(r):
    artist, title, label = (r + ["", "", ""])[:3]
    artist, title = (artist or "").strip(" -"), (title or "").strip(" -\"“”")
    if not artist or not title or len(title) > 150:
        return None
    return artist, title, (label or "").strip()


def today_au():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=10)).date().isoformat()


def load():
    with gzip.open(DATA, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def save(d):
    # mtime=0 keeps the file byte-identical when the data hasn't changed
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(DATA, "wb") as fh:
        fh.write(gzip.compress(raw, mtime=0))


CORRECTIONS = os.path.join(ROOT, "data", "corrections.json")


def corrected(playlists):
    """Apply data/corrections.json: drop wrongly dated copies, move playlists to their real date, add missing ones.
    Entries are matched by date + page link + title; the stored data file itself is never changed."""
    try:
        with open(CORRECTIONS, encoding="utf-8") as fh:
            c = json.load(fh)
    except FileNotFoundError:
        return playlists
    key = lambda e: (e["date"], e.get("url") or "", e.get("title") or "")
    drop = {key(e) for e in c.get("remove", [])}
    move = {key(e): e for e in c.get("redate", [])}
    out, used = [], set()
    for p in playlists:
        k = key(p)
        if k in drop:
            used.add(k); continue
        if k in move:
            used.add(k); p = dict(p, date=move[k]["to"], title=move[k].get("to_title") or p.get("title"))
        out.append(p)
    for k in (drop | set(move)) - used:
        print("corrections.json: no playlist matched", k)
    have = {key(p) for p in out}
    for e in c.get("add", []):
        p = {f: e.get(f) for f in ("date", "title", "url", "src", "abc_id", "sp", "rows")}
        if key(p) not in have:
            out.append(p)
    return out


def build(d, out_dir=ROOT):
    today = today_au()
    pls = sorted([p for p in corrected(d["playlists"]) if p["date"] <= today], key=lambda p: (p["date"], p.get("url") or ""))
    plays, play_page = [], []
    for pi, p in enumerate(pls):
        for r in p["rows"]:
            c = clean_row(r)
            if c:
                plays.append((*c, p["date"]))
                play_page.append(pi)

    mixed = collections.defaultdict(collections.Counter)
    for a, t, l, dt in plays:
        if not is_caps(a):
            mixed[norm(a)][a] += 1

    def smart_case(a):
        if not is_caps(a):
            return a
        m = mixed.get(norm(a))
        if m:
            return m.most_common(1)[0][0]
        out = []
        for w in a.split(" "):
            lw = w.lower()
            if lw in ("ft.", "ft", "feat.", "featuring", "and", "vs", "vs.", "x", "with"):
                out.append(lw)
            elif re.search(r"[0-9/.$!]", w) or not re.search(r"[AEIOUY]", w) or len(w) <= 2 and w not in (
                    "OF", "IN", "ON", "TO", "MY", "ME", "WE", "BY", "AT", "IS", "IT", "OR", "NO", "SO", "DO", "GO",
                    "UP", "US", "OH", "AN", "AS", "BE", "HE", "DJ"):
                out.append(w)
            else:
                out.append(re.sub(r"(^|[-(\"'])([a-z])", lambda m: m.group(1) + m.group(2).upper(), lw))
        s = " ".join(out)
        return s[:1].upper() + s[1:]

    groups, play_key = {}, []
    for a, t, l, dt in plays:
        k = (norm(a), norm_title(t))
        play_key.append(k)
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"a": collections.Counter(), "t": collections.Counter(), "l": collections.Counter(),
                             "n": 0, "first": dt, "last": dt, "y": collections.Counter()}
        g["y"][int(dt[:4])] += 1
        g["a"][smart_case(a)] += 1
        g["t"][t] += 1
        if l:
            g["l"][l] += 1
        g["n"] += 1
        g["first"] = min(g["first"], dt)
        g["last"] = max(g["last"], dt)

    rows = []
    for gk, g in groups.items():
        a = g["a"].most_common(1)[0][0]
        t = g["t"].most_common(1)[0][0]
        l = g["l"].most_common(1)[0][0] if g["l"] else ""
        rows.append((a, t, l, g["n"], g["first"], g["last"], g["y"], gk))
    rows.sort(key=lambda r: (fold(r[0]).lower(), fold(r[1]).lower()))

    artists, aidx, labels, lidx, songs = [], {}, [], {}, []
    for a, t, l, n, first, last, y, gk in rows:
        if a not in aidx:
            aidx[a] = len(artists); artists.append(a)
        if l not in lidx:
            lidx[l] = len(labels); labels.append(l)
        yc = ",".join(f"{yy - 1998}:{c}" for yy, c in sorted(y.items()))
        misc = 1 if (MISC.search(a + " | " + t) or SHOW.search(a) or SHOW_T.search(t)
                     or (re.search(r"(?i)\bcountdown\s*$|rock awards|rocktober", a) and not re.search(r"(?i)\bon countdown", a))) else 0
        sid = hashlib.sha1("\x1f".join(gk).encode("utf-8")).hexdigest()[:12]
        songs.append([aidx[a], t, lidx[l], n, int(first.replace("-", "")), int(last.replace("-", "")), yc, misc, sid])

    ridx = {r[7]: i for i, r in enumerate(rows)}
    tracks = collections.defaultdict(list)
    for k, pi in zip(play_key, play_page):
        tracks[pi].append(ridx[k])
    lists = []
    for pi, p in enumerate(pls):
        if not tracks[pi]:
            continue
        sp = p.get("sp")
        lists.append([int(p["date"].replace("-", "")), p.get("title") or "", p.get("url") or "", tracks[pi],
                      sp[1] if sp else "", (2 if sp[0] == "Guest programmer" else 1) if sp else 0])
    lists.sort(key=lambda x: (x[0], x[1]))

    # upcoming: posted by the ABC but not aired yet; rows carry their own text + song id (they don't count as plays)
    upcoming = []
    for p in sorted([p for p in d["playlists"] if p["date"] > today] + d.get("upcoming", []),
                    key=lambda p: (p["date"], p.get("title") or "")):
        if p["date"] <= today:
            continue
        rr = []
        for r in p["rows"]:
            c = clean_row(r)
            if c:
                rr.append([smart_case(c[0]), c[1], c[2], song_id(c[0], c[1])])
        if rr:
            sp = p.get("sp")
            upcoming.append([int(p["date"].replace("-", "")), p.get("title") or "", p.get("url") or "", rr,
                             sp[1] if sp else "", (2 if sp[0] == "Guest programmer" else 1) if sp else 0])

    yearly = collections.Counter(int(dt[:4]) for *_, dt in plays)
    meta = {"songs": len(rows), "plays": len(plays), "playlists": len(lists),
            "first": min(dt for *_, dt in plays), "last": max(dt for *_, dt in plays),
            "yearly": {y: yearly[y] for y in sorted(yearly)}, "updated": today, "upcoming": len(upcoming)}
    with open(os.path.join(out_dir, "songs.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "artists": artists, "labels": labels, "songs": songs}, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out_dir, "broadcasts.json"), "w", encoding="utf-8") as fh:
        json.dump({"lists": lists, "upcoming": upcoming}, fh, ensure_ascii=False, separators=(",", ":"))
    return meta


if __name__ == "__main__":
    import sys
    m = build(load(), sys.argv[1] if len(sys.argv) > 1 else ROOT)
    print(json.dumps({k: v for k, v in m.items() if k != "yearly"}))
