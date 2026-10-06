# /// script
# requires-python = ">=3.11"
# dependencies = ["curl_cffi"]
# ///
"""Daily remote-jobs scraper. Writes site/jobs.json, keeps jobs seen in the last 30 days."""
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

OUT = Path(__file__).parent / "site" / "jobs.json"
KEEP_DAYS = 30
NOW = int(time.time())

ENG = re.compile(
    r"engineer|developer|programmer|software|full[- ]?stack|front[- ]?end|back[- ]?end|devops|sre\b|"
    r"\bml\b|machine learning|\bai\b|llm|data scien|data engineer|mobile|\bios\b|android|platform|"
    r"infrastructure|architect|tech lead|\bcto\b|founding|web3|blockchain|qa\b|security engineer",
    re.I,
)
NOT_ENG = re.compile(
    r"mechanical|civil|electrical|structural|chemical|sales engineer|field engineer|hvac|manufacturing|"
    r"process engineer|quality engineer|revit|drafter|recruit|marketing|account exec",
    re.I,
)
INDIA_OK = re.compile(r"india|asia|apac|worldwide|anywhere|global|work from home|^remote$|^$", re.I)
COUNTRY_IN = re.compile(r"(?:^|[ ,/;(])IN(?:$|[ ,/;)])")  # only for sources that list ISO codes


def get(url, **kw):
    for attempt in range(3):
        r = requests.get(url, impersonate="chrome", timeout=40, **kw)
        if r.status_code != 429:
            break
        time.sleep(20 * (attempt + 1))
    r.raise_for_status()
    return r


def next_data(text):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    return json.loads(m.group(1))


def strip(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def job(source, title, company, url, location="", posted=None, salary="", tags=()):
    return {
        "source": source,
        "title": strip(title),
        "company": strip(company),
        "url": url,
        "location": strip(location) or "Remote",
        "posted": int(posted) if posted else None,
        "salary": salary or "",
        "tags": [t for t in tags if t][:6],
    }


def ts(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


# ---------- sources ----------

def remoteok():
    for j in get("https://remoteok.com/api").json()[1:]:
        sal = f"${j['salary_min']//1000}k–${j['salary_max']//1000}k" if j.get("salary_max") else ""
        yield job("Remote OK", j["position"], j["company"], j["url"], j.get("location"), j.get("epoch"), sal, j.get("tags", []))


def remotive():
    for j in get("https://remotive.com/api/remote-jobs?category=software-dev").json()["jobs"]:
        yield job("Remotive", j["title"], j["company_name"], j["url"], j.get("candidate_required_location"),
                  ts(j.get("publication_date", "")), j.get("salary", ""), j.get("tags", []))


def himalayas():
    for q in ["software engineer", "developer", "ai engineer", "full stack", "backend", "frontend", "machine learning", "devops"]:
        cursor = None
        for _ in range(8):
            url = f"https://himalayas.app/jobs/api/search?q={q.replace(' ', '+')}&worldwide=true&country=IN&limit=20"
            if cursor:
                url += f"&cursor={cursor}"
            d = get(url).json()
            for j in d["jobs"]:
                if j.get("pubDate") and NOW - j["pubDate"] > KEEP_DAYS * 86400:
                    continue
                loc = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
                sal = f"{j.get('currency') or '$'} {j['minSalary']:,}–{j['maxSalary']:,}" if j.get("minSalary") and j.get("maxSalary") else ""
                yield job("Himalayas", j["title"], j["companyName"], j["applicationLink"] or j["guid"], loc,
                          j.get("pubDate"), sal, j.get("categories", []))
            cursor = d.get("nextCursor")
            if not cursor or not d["jobs"]:
                break


def wwr():
    feeds = ["remote-full-stack-programming-jobs", "remote-back-end-programming-jobs",
             "remote-front-end-programming-jobs", "remote-devops-sysadmin-jobs"]
    for f in feeds:
        xml = get(f"https://weworkremotely.com/categories/{f}.rss").text
        for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
            tag = lambda t: html.unescape((re.search(rf"<{t}>(.*?)</{t}>", item, re.S) or [None, ""])[1]).strip()
            company, _, title = tag("title").partition(": ")
            pub = tag("pubDate")
            posted = int(datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").timestamp()) if pub else None
            yield job("We Work Remotely", title or company, company if title else "", tag("link") or tag("guid"),
                      tag("region"), posted, "", [tag("category")])


def arc():
    cats = ["", "python", "react", "javascript", "typescript", "node-js", "ai", "machine-learning", "golang",
            "java", "ruby-on-rails", "devops", "aws", "ios", "android", "react-native", "django", "php", "rust", "data-engineering"]
    for cat in cats:  # ?page= is ignored, category pages are not
        try:
            p = next_data(get(f"https://arc.dev/remote-jobs/{cat}".rstrip("/")).text)["props"]["pageProps"]
        except Exception:
            continue
        for j in p.get("arcJobs", []):
            loc = ", ".join(j.get("requiredCountries") or []) or "Worldwide"
            sal = f"${j['minHourlyRate']}–{j['maxHourlyRate']}/hr" if j.get("maxHourlyRate") else ""
            yield job("Arc", j["title"], "Arc client", f"https://arc.dev/remote-jobs/details/{j['urlString']}-{j['randomKey']}",
                      loc, j.get("postedAt"), sal, [c["name"] for c in j.get("categories", [])])
        for j in p.get("externalJobs", []):
            loc = ", ".join(j.get("requiredCountries") or []) or "Worldwide"
            yield job("Arc", j["title"], (j.get("company") or {}).get("name", ""),
                      f"https://arc.dev/remote-jobs/j/{j['urlString']}-{j['randomKey']}", loc, j.get("postedAt"), "",
                      [c["name"] for c in j.get("categories", [])])


def yc():
    paths = [f"role/{r}/{l}" for r in ["software-engineer", "full-stack", "backend", "frontend", "machine-learning",
                                       "devops", "android", "ios", "data-science"] for l in ["remote", "india"]]
    for path in paths:  # ?page= is ignored; each role/location slice is its own 40
        try:
            text = get(f"https://www.ycombinator.com/jobs/{path}").text
        except Exception:
            continue
        m = re.search(r'data-page="([^"]*)"', text)
        for j in json.loads(html.unescape(m.group(1)))["props"].get("jobPostings", []):
            yield job("YC Work at a Startup", j["title"], f"{j['companyName']} ({j.get('companyBatchName','')})",
                      "https://www.ycombinator.com" + j["url"], j.get("location"), None,
                      " · ".join(x for x in [j.get("salaryRange"), j.get("equityRange")] if x),
                      [j.get("roleSpecificType")] + (j.get("skills") or []))


def wellfound():
    plan = [("software-engineer", 30)] + [(r, 8) for r in ["full-stack-engineer", "backend-engineer", "frontend-engineer",
                                                          "machine-learning-engineer", "ai-engineer", "devops-engineer",
                                                          "mobile-engineer", "data-engineer"]]
    pages = [(r, p) for r, n in plan for p in range(1, n + 1)]
    for role, page in pages:
        try:
            d = next_data(get(f"https://wellfound.com/role/r/{role}?page={page}").text)
        except Exception:
            continue
        ap = d["props"]["pageProps"]["apolloState"]["data"]
        for v in ap.values():
            if v.get("__typename") != "StartupResult":
                continue
            for ref in v.get("highlightedJobListings", []):
                j = ap.get(ref["__ref"], {})
                if not j.get("remote"):
                    continue
                loc = ", ".join(j.get("acceptedRemoteLocationNames") or []) or "Remote (" + ", ".join(j.get("locationNames") or []) + ")"
                yield job("Wellfound", j["title"], v["name"], f"https://wellfound.com/jobs/{j['id']}-{j['slug']}",
                          loc, j.get("liveStartAt"), j.get("compensation", ""), [j.get("jobType")])


def cutshort():
    slugs = ["remote-jobs"] + [f"remote-{t}-jobs" for t in ["python", "javascript", "java", "nodejs", "reactjs", "react",
                                                          "angular", "golang", "php", "django", "aws", "devops",
                                                          "machine-learning", "data-science", "flutter", "android", "ios",
                                                          "typescript", "ruby-on-rails", "dotnet"]]
    for slug in slugs:  # ?page= is ignored; each tech page is its own newest 50
        try:
            d = next_data(get(f"https://cutshort.io/jobs/{slug}").text)
            jobs = d["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["data"]["pageData"]["jobs"]
        except Exception:
            continue
        for j in jobs:
            s = j.get("salaryRange") or {}
            sal = f"₹{s['min']/1e5:.0f}–{s['max']/1e5:.0f} LPA" if s.get("currency") == "INR" and s.get("max") else ""
            yield job("Cutshort", j["headline"], (j.get("companyDetails") or {}).get("name", ""), j["publicUrl"],
                      "Remote (India)", ts(j.get("postedOn") or j.get("createdAt") or ""), sal, j.get("allSkills", []))


def instahyre():
    for off in range(0, 35 * 30, 35):
        time.sleep(2)
        d = get(f"https://www.instahyre.com/api/v1/job_search?limit=35&offset={off}").json()
        for j in d.get("objects", []):
            if "Work From Home" not in (j.get("locations") or ""):
                continue
            yield job("Instahyre", j["title"], j["employer"]["company_name"], j["public_url"], j["locations"], None, "",
                      j.get("keywords", []))
        if not d.get("meta", {}).get("next"):
            return


SOURCES = [remoteok, remotive, himalayas, wwr, arc, yc, wellfound, cutshort, instahyre]


def main():
    old = {}
    if OUT.exists():
        old = {j["url"]: j for j in json.loads(OUT.read_text()).get("jobs", [])}

    def run(src):
        try:
            return src.__name__, list(src())
        except Exception as e:  # one broken site shouldn't kill the run
            return src.__name__, f"error: {type(e).__name__}: {e}"[:200]

    fresh, status = {}, {}
    with ThreadPoolExecutor(len(SOURCES)) as pool:
        results = list(pool.map(run, SOURCES))
    for name, got in results:
        if isinstance(got, str):
            status[name] = got
        else:
            n = 0
            for j in got:
                if not ENG.search(j["title"]) or NOT_ENG.search(j["title"]):
                    continue
                loc = j["location"].strip()
                j["india_ok"] = (bool(INDIA_OK.search(loc)) or j["source"] in ("Cutshort", "Instahyre")
                                 or (j["source"] in ("Arc", "YC Work at a Startup") and bool(COUNTRY_IN.search(loc))))
                n += j["url"] not in fresh
                fresh[j["url"]] = j
            status[name] = n
        print(f"{name:12} {status[name]}", file=sys.stderr)

    merged = {}
    for url, j in {**old, **fresh}.items():
        j["first_seen"] = old.get(url, {}).get("first_seen") or NOW
        if url in fresh or NOW - j["first_seen"] < KEEP_DAYS * 86400:
            merged[url] = j

    jobs = sorted(merged.values(), key=lambda j: max(j["first_seen"], j["posted"] or 0), reverse=True)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": NOW, "status": status, "jobs": jobs}, ensure_ascii=False))
    print(f"total {len(jobs)} ({len(fresh)} live today)", file=sys.stderr)


if __name__ == "__main__":
    main()
