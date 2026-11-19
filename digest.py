"""
Weekly paper digest
===================
1. FETCH  - last week's papers from arXiv (by category) and from journals
            (via the Crossref API, by ISSN).
   FILL   - abstracts missing from Crossref, looked up in OpenAlex and
            Semantic Scholar.
   DEDUPE - drop papers whose arXiv and journal versions both turn up.
2. SCORE  - each paper against the weighted keywords in topics.yaml.
3. SAVE   - docs/digests/<date>.json, which the website in docs/ displays.

Run locally without saving anything:
    python digest.py --dry-run        # writes digest.html to open in a browser
"""
import argparse
import datetime as dt
import html
import json
import re
import time
import unicodedata
from pathlib import Path

import feedparser
import requests
import yaml

HERE = Path(__file__).parent
CONFIG = yaml.safe_load((HERE / "topics.yaml").read_text(encoding="utf-8"))
SEEN_FILE = HERE / "seen.json"          # IDs already saved (avoids repeats)
DOCS = HERE / "docs"

TODAY = dt.date.today()
START = TODAY - dt.timedelta(days=CONFIG["days_back"])
FEATURED_SCORE = CONFIG.get("featured_score", 8)   # score that makes a "top pick"
WATCHED = "Watched authors"                        # section for watched-author papers


# ------------------------------------------------------------------
# 1. FETCH
# ------------------------------------------------------------------
def clean(text, sep=" "):
    """Remove the XML/HTML tags and entities Crossref leaves in titles and abstracts."""
    text = html.unescape(re.sub(r"<[^>]+>", sep, text or ""))
    return " ".join(text.split())


def fetch_arxiv(category):
    """All papers submitted to one arXiv category inside the date window."""
    window = f"[{START:%Y%m%d}0000 TO {TODAY:%Y%m%d}2359]"
    params = {
        "search_query": f"cat:{category} AND submittedDate:{window}",
        "max_results": 2000,
        "sortBy": "submittedDate",
    }
    resp = requests.get("https://export.arxiv.org/api/query", params=params, timeout=90)
    feed = feedparser.parse(resp.text)

    papers = []
    for e in feed.entries:
        arxiv_id = e.id.split("/abs/")[-1].rsplit("v", 1)[0]   # drop version "v2"
        papers.append({
            "id": "arxiv:" + arxiv_id,
            "title": " ".join(e.title.split()),                 # collapse line breaks
            "abstract": " ".join(e.summary.split()),
            "authors": ", ".join(a.name for a in e.get("authors", [])),
            "source": "arXiv",
            "category": category,
            "kind": "arxiv",
            "published": e.published[:10],                      # first submission date
            "url": e.link,
        })
    return papers


def crossref_date(item):
    """Publication date as 'YYYY-MM-DD' (or 'YYYY-MM' when Crossref has no day)."""
    parts = (item.get("published") or item.get("issued") or {}).get("date-parts", [[]])[0]
    parts = [p for p in parts if p]
    return "-".join(f"{p:04d}" if i == 0 else f"{p:02d}" for i, p in enumerate(parts))


PAGE_SIZE = 1000      # Crossref's maximum number of papers per request
MAX_PAGES = 10        # safety stop: never ask for more than 10 pages (10,000 papers)


def fetch_journals():
    """Recent papers from every journal in topics.yaml, via the Crossref API.

    Crossref returns at most 1000 papers per request, so we ask page by page.
    Each answer includes a "next-cursor" bookmark; sending it back gives the
    next page. We stop when a page comes back short (that was the last one).
    """
    filters = [f"from-created-date:{START}", f"until-created-date:{TODAY}"]
    filters += [f"issn:{issn}" for issn in CONFIG["journals"].values()]  # ISSNs are OR-ed
    params = {
        "filter": ",".join(filters),
        "rows": PAGE_SIZE,
        "select": "DOI,title,abstract,author,container-title,URL,published,issued,created",
        "cursor": "*",                    # "*" means: start at the first page
    }
    headers = {"User-Agent": f"paper-digest (mailto:{CONFIG['contact_email']})"}

    items = []
    for page in range(1, MAX_PAGES + 1):
        msg = requests.get("https://api.crossref.org/works", params=params,
                           headers=headers, timeout=90).json()["message"]
        items += msg["items"]
        print(f"Crossref page {page}: {len(msg['items'])} papers "
              f"({len(items)} of {msg['total-results']} so far)")
        if len(msg["items"]) < PAGE_SIZE or len(items) >= msg["total-results"]:
            break                             # last page reached
        params["cursor"] = msg["next-cursor"]  # bookmark for the next page
        time.sleep(1)                         # be polite to Crossref between pages
    else:
        print(f"Warning: stopped after {MAX_PAGES} pages; some journal papers were skipped")

    papers = []
    for item in items:
        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip()
                   for a in item.get("author", [])]
        abstract = clean(item.get("abstract", ""))
        abstract = re.sub(r"^Abstract\s*[:.]?\s*", "", abstract, flags=re.I)  # leading label
        papers.append({
            "id": "doi:" + item["DOI"].lower(),
            "title": clean((item.get("title") or ["(untitled)"])[0], sep=""),
            "abstract": abstract,
            "authors": ", ".join(authors),
            "source": (item.get("container-title") or ["Journal"])[0],
            "category": "",
            "kind": "journal",
            # the day the paper was registered = the day it went online; issue
            # dates (e.g. Elsevier's "2027-03") are only a fallback
            "published": (item.get("created") or {}).get("date-time", "")[:10]
                         or crossref_date(item),
            "url": item["URL"],
        })
    return papers


def fetch_all():
    """Run every fetcher; one failing source should not kill the whole digest."""
    papers = []
    for cat in CONFIG["arxiv_categories"]:
        try:
            papers += fetch_arxiv(cat)
        except Exception as err:
            print(f"arXiv {cat} failed: {err}")
        time.sleep(3)                     # arXiv asks for >= 3 s between API calls
    try:
        papers += fetch_journals()
    except Exception as err:
        print(f"Crossref failed: {err}")

    unique = {p["id"]: p for p in papers}   # de-duplicate cross-listed papers
    return list(unique.values())


# ------------------------------------------------------------------
# 1b. FILL MISSING ABSTRACTS
# ------------------------------------------------------------------
# Many publishers (APS, Elsevier, ...) do not give abstracts to Crossref, so
# those papers could only be scored on their title. Here we look the missing
# abstracts up by DOI in two free scholarly databases:
#   1. OpenAlex          (https://openalex.org)        - 50 DOIs per request
#   2. Semantic Scholar  (https://www.semanticscholar.org) - only for the rest
# Both also tell us whether the paper has an arXiv version; we store that
# in paper["arxiv_id"] so duplicates can be recognised later.
# If either service is down, the digest still runs, just with fewer abstracts.

ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.I)


def abstract_from_inverted_index(index):
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not index:
        return ""
    words = {pos: word for word, positions in index.items() for pos in positions}
    return " ".join(words[i] for i in sorted(words))


RETRY_WAITS = [10, 30, 60]   # seconds to wait before each retry after "too many requests"


def ask(service, method, url, **kwargs):
    """Send one request; on "too many requests" (429) or a server error (5xx),
    wait and try again (10 s, 30 s, 60 s). Returns the answer if it succeeded,
    otherwise prints the reason in the log and returns None."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            resp = requests.request(method, url, timeout=90, **kwargs)
        except requests.RequestException as err:      # no answer at all (network problem)
            print(f"{service}: no answer ({err})")
            return None
        if resp.status_code == 200:
            return resp
        retryable = resp.status_code == 429 or resp.status_code >= 500
        if not retryable or attempt == len(RETRY_WAITS):
            reason = " ".join(resp.text.split())[:200]  # first part of the error message
            print(f"{service} answered {resp.status_code}: {reason}")
            return None
        wait = RETRY_WAITS[attempt]
        print(f"{service} answered {resp.status_code}; waiting {wait} s and retrying")
        time.sleep(wait)


def fill_from_openalex(papers):
    """Fill abstracts (and arXiv IDs) for `papers` from OpenAlex, 50 at a time."""
    by_doi = {p["id"][4:]: p for p in papers}          # "doi:10.1103/..." -> "10.1103/..."
    dois = list(by_doi)
    for start in range(0, len(dois), 50):
        chunk = dois[start:start + 50]
        params = {
            "filter": "doi:" + "|".join(chunk),           # "|" means OR in OpenAlex
            "per-page": 50,
            "select": "doi,abstract_inverted_index,locations",
            "mailto": CONFIG["contact_email"],            # OpenAlex's "polite pool"
        }
        resp = ask("OpenAlex", "GET", "https://api.openalex.org/works", params=params)
        if resp is None:
            print("OpenAlex: stopping for this week")
            return                                         # the reason is already in the log
        for work in resp.json().get("results", []):
            doi = (work.get("doi") or "").lower().replace("https://doi.org/", "")
            paper = by_doi.get(doi)
            if not paper:
                continue
            if not paper["abstract"]:
                paper["abstract"] = abstract_from_inverted_index(work.get("abstract_inverted_index"))
            for loc in work.get("locations") or []:       # look for an arXiv copy
                found = ARXIV_ID.search(loc.get("landing_page_url") or "")
                if found:
                    paper["arxiv_id"] = found.group(1)
                    break
        time.sleep(1)                                      # be polite between requests


def fill_from_semantic_scholar(papers):
    """Fill remaining abstracts (and arXiv IDs) from Semantic Scholar, 500 at a time."""
    for start in range(0, len(papers), 500):
        chunk = papers[start:start + 500]
        resp = ask("Semantic Scholar", "POST",
                   "https://api.semanticscholar.org/graph/v1/paper/batch",
                   params={"fields": "abstract,externalIds"},
                   json={"ids": ["DOI:" + p["id"][4:] for p in chunk]})
        if resp is None:
            print("Semantic Scholar: skipping it this week")
            return                                         # the reason is already in the log
        for paper, info in zip(chunk, resp.json()):        # answers come back in the same order
            if not info:
                continue                                   # paper not in their database
            if not paper["abstract"] and info.get("abstract"):
                paper["abstract"] = info["abstract"]
            arxiv = (info.get("externalIds") or {}).get("ArXiv")
            if arxiv and not paper.get("arxiv_id"):
                paper["arxiv_id"] = arxiv
        time.sleep(3)


def fill_missing_abstracts(papers):
    """Look up abstracts for journal papers that arrived without one."""
    journal = [p for p in papers if p["kind"] == "journal"]
    missing = [p for p in journal if not p["abstract"]]
    print(f"{len(missing)} of {len(journal)} journal papers have no abstract")
    if not missing:
        return

    try:
        fill_from_openalex(journal)       # all journal papers: also finds arXiv versions
    except Exception as err:
        print(f"OpenAlex failed: {err}")
    still_missing = [p for p in missing if not p["abstract"]]
    try:
        fill_from_semantic_scholar(still_missing)
    except Exception as err:
        print(f"Semantic Scholar failed: {err}")

    for p in missing:                      # tidy what we found, as for Crossref abstracts
        p["abstract"] = re.sub(r"^Abstract\s*[:.]?\s*", "", clean(p["abstract"]), flags=re.I)
    filled = sum(1 for p in missing if p["abstract"])
    print(f"Abstracts filled: {filled} of {len(missing)} missing "
          f"({len(missing) - len(still_missing)} from OpenAlex, "
          f"{filled - (len(missing) - len(still_missing))} from Semantic Scholar)")


# ------------------------------------------------------------------
# 1c. REMOVE DUPLICATES (same work on arXiv and in a journal)
# ------------------------------------------------------------------
# A preprint often appears on arXiv first and in a journal weeks later.
# We recognise the pair in two ways:
#   - the journal paper's arXiv ID (found by fill_missing_abstracts), which
#     also works across weeks because seen.json stores every arXiv ID shown;
#   - an identical title within this week's papers (ignoring case, spaces,
#     LaTeX and punctuation), for journal papers without a known arXiv ID.
# When both versions arrive in the same week we keep the journal version
# (it has the DOI) and borrow the arXiv abstract if the journal had none.

def title_key(title):
    """'Spin Ice in Dy$_2$Ti$_2$O$_7$!' -> 'spiniceindy2ti2o7': letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def remove_duplicates(papers, seen):
    arxiv_by_id = {p["id"]: p for p in papers if p["kind"] == "arxiv"}
    arxiv_by_title = {title_key(p["title"]): p for p in arxiv_by_id.values()}
    drop = set()                           # ids of papers to remove
    by_id = by_title = 0

    for p in papers:
        if p["kind"] != "journal":
            continue
        twin_id = "arxiv:" + p["arxiv_id"] if p.get("arxiv_id") else None

        if twin_id and twin_id in seen:    # preprint already shown in an earlier week
            drop.add(p["id"])
            by_id += 1
            continue

        twin = arxiv_by_id.get(twin_id)   # preprint arrived this same week?
        matched_by_title = False
        if not twin:
            key = title_key(p["title"])
            if len(key) > 20:              # skip very short titles like "Erratum"
                twin = arxiv_by_title.get(key)
                matched_by_title = twin is not None
        if twin:
            if not p["abstract"]:
                p["abstract"] = twin["abstract"]
            p["arxiv_id"] = twin["id"][6:]
            drop.add(twin["id"])           # keep the journal version
            by_title += matched_by_title
            by_id += not matched_by_title

    if drop:
        print(f"Duplicates removed: {len(drop)} ({by_id} by arXiv ID, {by_title} by title)")
    return [p for p in papers if p["id"] not in drop]


# ------------------------------------------------------------------
# 2. SCORE
# ------------------------------------------------------------------
def found(term, text):
    """True if `term` starts at a word boundary in `text`.

    Stems still work ("superconduct" matches "superconductor"), but a short
    keyword no longer matches in the middle of another word
    ("inas" used to match "kinase").
    """
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()), text) is not None


def plain(text):
    """Lower-case and strip accents, so 'Schäpers' matches 'Schapers'."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def no_latex(text):
    """Remove LaTeX markup so formulas read as plain text:
    'Dy$_2$Ti$_2$O$_7$' -> 'Dy2Ti2O7',  '$\\alpha$-RuCl$_3$' -> '-RuCl3'."""
    text = re.sub(r"\\[a-zA-Z]+", "", text)       # commands such as \mathrm, \alpha
    return re.sub(r"[$_^{}\\]", "", text)          # the symbols $ _ ^ { } \


def squeeze(text):
    """Letters and digits only: 'Dy 2 Ti 2 O 7' -> 'dy2ti2o7'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def has_term(term, text, squeezed):
    """Normal keywords must start at a word boundary (see found()).
    Keywords containing a digit are chemical formulas ('dy2ti2o7', 'mnbi2te4'):
    journals often space them out ('Dy 2 Ti 2 O 7'), so for those we also
    compare with all spaces and symbols removed."""
    if found(term, text):
        return True
    return any(c.isdigit() for c in term) and squeeze(term) in squeezed


def topic_match(title, text, terms):
    """Return (score, matched keywords) for one topic. Title hits count double."""
    title_sq, text_sq = squeeze(title), squeeze(text)
    matched = [t for t in terms if has_term(t, text, text_sq)]
    # A keyword written without a number in topics.yaml counts as weight 1
    score = sum((terms[t] or 1) * (2 if has_term(t, title, title_sq) else 1)
                for t in matched)
    return score, matched


def classify(paper):
    """Score one paper. Returns None if it is not relevant, else the paper plus
    topic, score, matched keywords, watched authors and a top-pick flag."""
    title = no_latex(paper["title"]).lower()
    text = title + " " + no_latex(paper["abstract"]).lower()

    hits = {}                                    # topic -> (score, matched keywords)
    for topic, terms in CONFIG["topics"].items():
        score, matched = topic_match(title, text, terms)
        if score > 0:
            hits[topic] = (score, matched)

    authors = plain(paper["authors"])
    watched = [name for name in CONFIG.get("watch_authors", []) if plain(name) in authors]
    # Another researcher can share the same name (a "Yishu Wang" in medicine
    # once appeared). A watched author only counts if the paper also matches at
    # least one of your keywords; papers by your watched authors will always
    # mention magnetism, neutrons, etc. Affiliations can't be used instead:
    # arXiv doesn't provide them and most publishers don't send them to Crossref.
    if not hits:
        watched = []

    total = sum(s for s, _ in hits.values()) + (5 if watched else 0)
    if total < CONFIG["min_score"] and not watched:
        return None

    # File the paper under its best-matching topic
    topic = max(hits, key=lambda t: hits[t][0])
    keywords = list(dict.fromkeys(k for _, matched in hits.values() for k in matched))
    paper.update(topic=topic, score=total, matched=keywords, watched=watched,
                 featured=bool(watched) or total >= FEATURED_SCORE)
    return paper


def group_by_topic(papers):
    """Group by topic, best first, capped per topic. Empty topics are dropped."""
    groups = {t: [] for t in list(CONFIG["topics"]) + [WATCHED]}
    for p in papers:
        groups.setdefault(p["topic"], []).append(p)

    result = {}
    for topic, items in groups.items():
        items.sort(key=lambda p: p.get("published", ""), reverse=True)   # newest first
        items.sort(key=lambda p: -p["score"])                            # then best first
        if items:
            result[topic] = items[:CONFIG["max_per_topic"]]
    return result


# ------------------------------------------------------------------
# 3. SAVE
# ------------------------------------------------------------------
def build_html(by_topic):
    """Simple HTML preview for --dry-run (the real site is docs/index.html)."""
    total = sum(len(v) for v in by_topic.values())
    parts = [f"<div style='font-family:Georgia,serif;max-width:720px'>"
             f"<h2>Papers {START:%b %d} – {TODAY:%b %d}: {total} matches</h2>"]

    for topic, papers in by_topic.items():
        parts.append(f"<h3 style='border-bottom:1px solid #ccc'>{html.escape(topic)} "
                     f"({len(papers)})</h3>")
        for p in papers:
            abstract = p["abstract"][:400] + ("…" if len(p["abstract"]) > 400 else "")
            parts.append(
                f"<p><a href='{p['url']}' style='font-size:15px'><b>{html.escape(p['title'])}</b></a><br>"
                f"<span style='color:#555;font-size:12px'>{html.escape(p['authors'][:200])}"
                f" · <i>{html.escape(p['source'])}</i> · {p['published']} · score {p['score']}</span><br>"
                f"<span style='font-size:13px'>{html.escape(abstract)}</span></p>")
    parts.append("</div>")
    return "\n".join(parts), total


def load_existing(path):
    """Papers already saved today, so a second run adds to the digest
    instead of replacing it."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    papers = []
    for topic, items in data.get("topics", {}).items():
        for p in items:
            p.setdefault("topic", topic)
            p.setdefault("published", "")
            papers.append(p)
    return papers


FIELDS = ["id", "title", "authors", "abstract", "source", "category", "kind",
          "url", "published", "score", "matched", "watched", "featured", "arxiv_id"]


def save_digest(path, by_topic):
    """Write the digest JSON and update the archive index docs/digests.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = sum(len(v) for v in by_topic.values())
    data = {
        "date": str(TODAY),
        "period": f"{START} to {TODAY}",
        "total": total,
        "topic_order": list(CONFIG["topics"]) + [WATCHED],   # keeps topic colours stable
        "topics": {topic: [{k: p.get(k) for k in FIELDS if k in p} for p in papers]
                   for topic, papers in by_topic.items()},
    }
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    # The index lists every digest; replace today's entry so re-runs stay accurate.
    index_file = DOCS / "digests.json"
    index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else []
    index = [d for d in index if d["date"] != str(TODAY)]
    index.append({"date": str(TODAY), "count": total})
    index.sort(key=lambda d: d["date"], reverse=True)
    index_file.write_text(json.dumps(index, indent=2), encoding="utf-8")

    print(f"Saved {total} papers to {path}")


# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="write digest.html, save nothing")
    args = parser.parse_args()

    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    fresh = [p for p in fetch_all() if p["id"] not in seen]
    print(f"Fetched {len(fresh)} new papers")
    fill_missing_abstracts(fresh)
    fresh = remove_duplicates(fresh, seen)

    picked = [p for p in map(classify, fresh) if p]
    print(f"{len(picked)} match your topics")

    if args.dry_run:
        body, total = build_html(group_by_topic(picked))
        (HERE / "digest.html").write_text(body, encoding="utf-8")
        print(f"Wrote digest.html with {total} papers")
        return

    path = DOCS / "digests" / f"{TODAY}.json"
    merged = {p["url"]: p for p in load_existing(path) + picked}   # same-day re-run: keep both
    by_topic = group_by_topic(list(merged.values()))
    save_digest(path, by_topic)

    # Remember only what was actually saved
    seen |= {p["id"] for items in by_topic.values() for p in items if "id" in p}
    # Also remember the arXiv ID of each saved journal paper, so its preprint
    # is not shown again if it is posted to arXiv later.
    seen |= {"arxiv:" + p["arxiv_id"] for items in by_topic.values() for p in items
             if p.get("arxiv_id")}
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0))


if __name__ == "__main__":
    main()
