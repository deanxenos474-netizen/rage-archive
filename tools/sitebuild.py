"""Rebuild songs.json and broadcasts.json from data/playlists.json.gz.

Standard library only, so it runs on GitHub Actions without installing anything.
Song ids are a hash of the normalised artist + title, so covers, videos and merges stay attached across rebuilds.
"""
import collections, datetime, gzip, hashlib, json, os, re, unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "playlists.json.gz")
CHART_NUMBERS = os.path.join(ROOT, "data", "chart_numbers.json")

FEAT = re.compile(r"\s+(?:ft\.?|feat\.?|featuring|with)\s+", re.I)
CONN = re.compile(r"\b(ft\.?|feat\.?|featuring|and|vs\.?|x|with|presents|pres\.?)\b")
LINKS = re.compile(r"(?<!\S)(_and_|&&|from|of|on|versus|present|aka|a\.k\.a\.|f/|t\.|featuring\.?|feat|featuing|feauturing)(?!\S)")
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
    # all capitals, also counting older ABC spellings in half capitals: "The WEEKND", "PAUL McCARTNEY",
    # "FOO FIGHTERS - Live from ..." (only the name before " - " counts; a leading "The" and Mc/Mac are ignored)
    core = re.sub(r"(?i)^the\s+", "", a.split(" - ")[0])
    core = re.sub(r"\bMa?c(?=[A-Z]{2})", "", core)
    core = CONN.sub("", core)
    before = core == core.upper()
    # small linking words the ABC writes in lower case between capitalised names ("JOHN LYDON _and_ JAH WOBBLE from
    # PUBLIC IMAGE LTD", "B-REAL of CYPRESS HILL"), and words that are mixed case by design ("9th", "B.o.B", "A$AP")
    core = LINKS.sub("", core)
    core = " ".join(w for w in core.split() if not re.search(r"[0-9.$]", w))
    if not re.search(r"[A-Za-z]", core):
        return before
    return core == core.upper()


# a playlist's name type -> broadcasts.json kind: 0 = a regular episode's name ("Charts"), 1 = special, 2 = guest programmer
KIND = {"Show": 0, "Special": 1, "Guest programmer": 2}


# artists with a set spelling that the listings don't always use ("MALI JOSE", "Asap Rocky", "Ty Dolla Sign").
# Applied to the shown name after the capitals are fixed, so song ids stay the same.
STYLED = [(re.compile(p, re.I), r) for p, r in [
    (r"\bmali jo[s$]e\b", "MALI JO$E"),
    (r"\ba[s$]ap(?= )", "A$AP"),
    (r"\btravi[s$] scott\b", "Travis Scott"),
    (r"\bbbno[s$](?![\w$])", "bbno$"),
    (r"\bty dolla (?:\$ign|sign)\b", "Ty Dolla $ign"),
    (r"\bmanu crook[s$](?![\w$])", "Manu Crook$"),
    (r"^angel du[s$]t\b", "Angel Du$t"),   # the band; "Savage featuring Angel Dust" is someone else
    (r"\bbeyonc[eé](?! knowles)\b", "Beyoncé"),
    (r"\br[oö]yksopp\b", "Röyksopp"),
    (r"\bemeli sand[eé]\b", "Emeli Sandé"),
    (r"(?<![\w])[aá]sgeir\b", "Ásgeir"),
    (r"\btouch[eé] amor[eé]\b", "Touché Amoré"),
    (r"\bgabriel garz.n-montano\b", "Gabriel Garzón-Montano"),
    (r"^the models\b", "Models"),   # never "The Models" (a few old listings wrote it so)
    (r"\b300skullsandcounting\b", "300SkullsAndCounting"),
    (r"(?<![\w$])[s$]iggy rett[s$](?![\w$])", "$iggy Rett$"),
    (r"\b1 ?tbsp\b", "1tbsp"),
]]


def styled(a):
    for rx, rep in STYLED:
        a = rx.sub(lambda m, r=rep: r, a)
    return a


def clean_row(r):
    artist, title, label = (r + ["", "", ""])[:3]
    artist, title = (artist or "").strip(" -"), (title or "").strip(" -\"“”")
    if not artist or not title or len(title) > 150:
        return None
    artist, title = unsplit(artist, title)
    artist, title = NOTE.sub(" ", artist).strip(), NOTE.sub(" ", title).strip()   # "Shaggy   ***Please Note: ...***"
    artist = unthe(artist)   # "POGUES, THE ft. KIRSTY MacCOLL" -> "THE POGUES ft. KIRSTY MacCOLL"
    artist = re.sub(r"\s*[,;:]+$", "", artist)   # "The WEEKND," (a stray comma left after the name)
    artist = re.sub(r"(?i)\bBeyonc(?=\s|$)", "Beyoncé", artist)   # a listing that cut off the "é"
    return artist, title, (label or "").strip()


# the old ABC pages were split at the first hyphen, so a hyphen inside a title pushed the rest into the artist:
# "Ray Of Light (Sasha Ultra" / "Violet Mix) - Madonna", "Woo" / "Hah!! (Got You All In Check) - BUSTA RHYMES".
# A "live" note can also come first: "Live On Recovery - The Whitlams" -> "The Whitlams - Live On Recovery".
# old ABC listings put "The" last: "POGUES, THE ft. ...", "Chainsmokers, The ft. Halsey", "Weeknd, The, Madonna".
# Only when "The" ends the name or is followed by a credit/separator, so "Tyler, The Creator" stays as it is.
UNTHE = re.compile(r"^(?P<x>[^,]+?),\s*(?P<the>the)(?P<rest>(?:\s+(?:ft\.?|feat\.?|featuring|with|and|&|_and_|vs\.?|x|conducted|presents|live|-)(?=\s|$).*|\s*\(.*|\s*,.*)?)\s*$", re.I)


def unthe(a):
    m = UNTHE.match(a)
    if not m:
        return a
    the = "THE" if m.group("the").isupper() else "The"
    return f"{the} {m.group('x').strip()}{m.group('rest')}"


NOTE = re.compile(r"\s*\*\*\*\s*(please note|abc tv)[^*]*\*\*\*\s*|\s*\(\s*missing label\s*\)\s*", re.I)   # also ABC's "(missing label)" note
HY_PAREN = re.compile(r"^([^()]*?\))\s*-\s*(\S.*)$")
HY_REST = re.compile(r"^(\S[^-]*?)\s+-\s+(\S.*)$")
QUAL = re.compile(r"(live|in concert|from the video)\b", re.I)
QUAL_FIRST = re.compile(r"^((?:live|in concert|from the video)\b.*?)\s+-\s+(\S.*)$", re.I)


def unsplit(artist, title):
    m = HY_PAREN.match(artist)
    if m and title.count("(") > title.count(")"):
        return m.group(2), title + "-" + m.group(1)
    m = HY_REST.match(artist)
    if m and len(title) <= 3 and title[:1].isalpha() and not (len(title) > 1 and title.isupper()) \
            and not QUAL.match(m.group(2)):    # not "Powderfinger - Live on Recovery" / "JC", nor "EPK"
        return m.group(2), title + "-" + m.group(1)
    m = QUAL_FIRST.match(artist)
    if m:
        return m.group(2) + " - " + m.group(1), title
    return artist, title


# a chart position written before the artist in countdown playlists: "3. Europe", "01.GOTYE". Not "7.30 Report",
# and dash forms ("1 - The Vandals", "4-3-2-1 - Thursday") are left alone as they may be part of a name
RANK = re.compile(r"^\s*(the\s+)?(\d{1,2})\.\s*(?!\d)(\S.*)$", re.I)   # also the ABC's "THE 6. BANGLES"


# library-style names: "Proclaimers, The" -> "The Proclaimers", "Tribe Called Quest, A" -> "A Tribe Called Quest"
INVERTED = re.compile(r"^(.+?)\s*,\s*(the|a)\s*$", re.I)


# 1999 top 50 playlists put the number before the title instead: "Smash Mouth - 31.All Star".
# Real titles that look like that are kept as they are.
TRANK = re.compile(r"^\s*(\d{1,2})\.(?:\s+|(?!\d)|(?=\d{1,2}\s*[ap]m\b))(\S.*)$", re.I)   # also "9. 2 Times" and "22.9pm ('Til I Come)"
KEEP_TITLES = {"7.X"}   # Hviske


# the Hottest 100 of 1999 (Feb 2000) pages: "No.9????Last Kiss" (the ? are a garbled separator)
HRANK = re.compile(r"^\s*No\.\s*(\d{1,3})\?{2,}\s*(\S.*)$")


def split_title_rank(title):
    m = HRANK.match(title)
    if m:
        return int(m.group(1)), m.group(2).strip()
    m = TRANK.match(title)
    return (int(m.group(1)), m.group(2).strip()) if m and title.strip() not in KEEP_TITLES else (0, title)


# junk the old ABC pages left around artist names
JUNK_TIME = re.compile(r"^\s*\d{1,2}:\d{2}\s*[ap]m\s+", re.I)          # "04:00am\r\n\r\nDAVID ESSEX"
JUNK_V = re.compile(r"^v(?=[A-Z][a-z])")                                # "vSmash Mouth" (Aug-Oct 1999 pages)
JUNK_INTOP = re.compile(r"^(.*?)\s+(\d{1,2})\s+in\s+top\s*50\s*$", re.I)  # "Westlife  19 in Top 50"
JUNK_END = re.compile(r"\s+\W?\s*end of (the )?(top 50|triple j's hottest 100)\s*$", re.I)   # "Eiffel 65   End of the Top 50"


def split_rank(artist):
    """(chart number or 0, artist as it should read): also turns "Proclaimers, The" round"""
    artist = JUNK_V.sub("", JUNK_TIME.sub("", artist)).strip()
    artist = JUNK_END.sub("", artist).strip()
    m = JUNK_INTOP.match(artist)
    if m:
        return int(m.group(2)), m.group(1).strip()
    m = RANK.match(artist)
    rank, artist = (int(m.group(2)), ((m.group(1) or "") + m.group(3)).strip()) if m else (0, artist)
    m = INVERTED.match(artist)
    if m:
        artist = m.group(2).capitalize() + " " + m.group(1)
    return rank, artist


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
    # rows: single rows the ABC's page got wrong (e.g. the label glued to the artist), matched by date + exact row;
    # "to" is one row, or a list of rows to put in its place (to add songs the owner chose to add)
    fixes = {}
    for e in c.get("rows", []):
        fixes.setdefault(e["date"], {})[tuple(e["from"])] = e["to"]
    hit = set()
    for i, p in enumerate(out):
        f = fixes.get(p["date"])
        if f and any(tuple((r + ["", "", ""])[:3]) in f for r in p["rows"]):
            rows = []
            for r in p["rows"]:
                k = tuple((r + ["", "", ""])[:3])
                if k in f:
                    hit.add((p["date"], k))
                    to = f[k]
                    if to and isinstance(to[0], list):   # several rows: a fix that also adds rows next to this one
                        rows.extend(list(x) for x in to); continue
                    r = list(to)
                rows.append(r)
            out[i] = dict(p, rows=rows)
    for dte, f in fixes.items():
        for k in f:
            if (dte, k) not in hit:
                print("corrections.json: no row matched", dte, k)
    have = {key(p) for p in out}
    for e in c.get("add", []):
        p = {f: e.get(f) for f in ("date", "title", "url", "src", "abc_id", "sp", "rows")}
        if key(p) not in have:
            out.append(p)
    return [undoubled(p) for p in out]


def undoubled(p):
    """Some ABC pages list the whole night two or three times over (the same block pasted again, same times):
    keep one copy. Only when every copy is identical and at least 6 rows long."""
    rows = p["rows"]
    n = len(rows)
    for k in (3, 2):
        if n % k == 0 and n // k >= 6 and all(rows[i * n // k:(i + 1) * n // k] == rows[:n // k] for i in range(1, k)):
            return dict(p, rows=rows[:n // k])
    return p


def chart_numbers(pls):
    """data/chart_numbers.json: chart numbers read from the ABC's own archived playlist pages (Wayback copies), for
    countdowns whose stored rows lost their numbers. Each entry names a playlist (date + page link) and its numbered
    rows as stored ([artist, title, number]); they are matched in running order. "top50": true marks an ARIA Top 50,
    which gets the on-screen Top 50 look; Hottest 100s and end-of-year charts only get the numbers.
    Returns {playlist index: {row index: (number, top50)}}."""
    try:
        with open(CHART_NUMBERS, encoding="utf-8") as fh:
            c = json.load(fh)
    except FileNotFoundError:
        return {}
    where = {(p["date"], p.get("url") or ""): i for i, p in enumerate(pls)}
    out = {}
    for e in c.get("lists", []):
        pi = where.get((e["date"], e.get("url") or ""))
        if pi is None:
            print("chart_numbers.json: no playlist matched", e["date"], e.get("url")); continue
        rows, want, j, got = pls[pi]["rows"], e["rows"], 0, {}
        for ri, r in enumerate(rows):
            if j < len(want) and (r + ["", ""])[:2] == want[j][:2]:
                got[ri] = (want[j][2], bool(e.get("top50"))); j += 1
        if j < len(want):
            print("chart_numbers.json:", e["date"], "matched", j, "of", len(want), "rows")
        out[pi] = got
    return out


def build(d, out_dir=ROOT):
    today = today_au()
    pls = sorted([p for p in corrected(d["playlists"]) if p["date"] <= today], key=lambda p: (p["date"], p.get("url") or ""))
    plays, play_page, play_rank = [], [], []
    numbers = chart_numbers(pls)
    for pi, p in enumerate(pls):
        for ri, r in enumerate(p["rows"]):
            c = clean_row(r)
            if c:
                rank, a = split_rank(c[0])
                trank, title = split_title_rank(c[1])
                plays.append((a, title, c[2], p["date"]))
                play_page.append(pi)
                n, top50 = numbers.get(pi, {}).get(ri, (0, False))
                rank = n or rank or trank
                play_rank.append([rank, 1] if rank and top50 else rank)

    mixed = collections.defaultdict(collections.Counter)
    for a, t, l, dt in plays:
        if not is_caps(a):
            mixed[norm(a)][a] += 1

    # Mc/Mac names as written somewhere in the data ("TATE McRAE", "Angie McMahon"), so "TATE MCRAE" becomes Tate McRae too
    mcmap = {}
    for a, t, l, dt in plays:
        for w in re.findall(r"\bMa?c[A-Z][A-Za-z']+", a):
            k = 3 if w.startswith("Mac") else 2
            mcmap.setdefault(w.lower(), w[:k] + w[k:].capitalize() if w[k:].isupper() else w)

    def smart_case(a):
        if not is_caps(a):
            return a
        m = mixed.get(norm(a))
        if m:
            return m.most_common(1)[0][0]
        head, sep, tail = a.partition(" - ")   # "FOO FIGHTERS - Live from 'Skin And Bones' DVD": only the name changes
        if tail == tail.upper():   # "THE VINES - LIVE ON TRIPLE J TV": all of it is in capitals, so all of it changes
            head, sep, tail = a, "", ""
        out = []
        for w in head.split(" "):
            lw = w.lower()
            if lw in ("ft.", "ft", "feat.", "featuring", "and", "vs", "vs.", "x", "with") or w in (
                    "_and_", "from", "of", "on", "versus", "present", "aka", "a.k.a.", "f/", "t.",
                    "featuring.", "feat", "featuing", "feauturing"):   # written in lower case in the listing: stays so
                out.append({"_and_": "and", "_&_": "&"}.get(lw, lw))   # the ABC's "_and_" stands in for "and"
            elif w == "_&_":
                out.append("&")
            elif re.match(r"^Ma?c[A-Z]{2}", w):   # "McCARTNEY" -> McCartney, "MacCOLL" -> MacColl
                k = 3 if w.startswith("Mac") else 2
                out.append(w[:k] + w[k:].capitalize())
            elif out and lw in ("jr", "jr.", "sr", "sr."):   # "GARY CLARK JR" -> Gary Clark Jr. (not a first word: SR 71 is a band)
                out.append(lw[:1].upper() + lw[1:2] + ".")
            elif re.search(r"[0-9/.$!]", w) or not re.search(r"[AEIOUY]", w) or len(w) <= 2 and w not in (
                    "OF", "IN", "ON", "TO", "MY", "ME", "WE", "BY", "AT", "IS", "IT", "OR", "NO", "SO", "DO", "GO",
                    "UP", "US", "OH", "AN", "AS", "BE", "HE", "DJ"):
                out.append(w)
            elif lw in mcmap:
                out.append(mcmap[lw])
            else:
                # capital after a hyphen, bracket, quote or apostrophe (O'Brien, Kee'Ahn), but not in "'s", "n't", "'ll"
                out.append(re.sub(r"(^|[-(\"]|'(?!(?:s|t|d|m|ll|re|ve)\b))([a-z])", lambda m: m.group(1) + m.group(2).upper(), lw))
        s = " ".join(out) + sep + tail
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
        g["a"][styled(smart_case(a))] += 1
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
    tracks, ranks = collections.defaultdict(list), collections.defaultdict(list)
    for k, pi, rank in zip(play_key, play_page, play_rank):
        if rank:   # [position in the running order, chart number] (+ 1 when it's an ARIA Top 50: the on-screen Top 50 look)
            ranks[pi].append([len(tracks[pi])] + (rank if isinstance(rank, list) else [rank]))
        tracks[pi].append(ridx[k])
    lists = []
    for pi, p in enumerate(pls):
        if not tracks[pi]:
            continue
        sp = p.get("sp")
        lists.append([int(p["date"].replace("-", "")), p.get("title") or "", p.get("url") or "", tracks[pi],
                      sp[1] if sp else "", KIND.get(sp[0], 1) if sp else 0]
                     + ([ranks[pi]] if ranks[pi] else []))
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
                rank, a = split_rank(c[0])
                trank, title = split_title_rank(c[1])
                rank = rank or trank
                rr.append([styled(smart_case(a)), title, c[2], song_id(a, title)] + ([rank] if rank else []))
        if rr:
            sp = p.get("sp")
            upcoming.append([int(p["date"].replace("-", "")), p.get("title") or "", p.get("url") or "", rr,
                             sp[1] if sp else "", KIND.get(sp[0], 1) if sp else 0])

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
