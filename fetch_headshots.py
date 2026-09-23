#!/usr/bin/env python3
"""
One-time scrape: Premier League player headshots -> headshots.json

Run from the repo root:
    pip install playwright
    playwright install chromium
    python fetch_headshots.py              # opens Chrome and clicks through every page by itself
    python fetch_headshots.py --manual     # you click "next" in the browser, press Enter after each page
    python fetch_headshots.py --headless   # same as the first, without a visible window

Output (next to this script):
    headshots.json            {"players": {"<fpl player id>": "<headshot url>", ...}, ...}
    headshots_unmatched.txt   headshots whose name couldn't be matched to an FPL player

How matching works
  1. By ID: the number in the image URL (e.g. .../232980.png) is compared with each
     FPL player's "code". If these line up, names never need to be guessed.
  2. By name, for anything left: accents stripped, full name first, then FPL's short
     display name when that's unique.

Headshot images belong to the Premier League. This is for a private league app.
"""
import json, re, sys, time, unicodedata, urllib.request, urllib.error
from datetime import datetime, timezone

PLAYERS_URL = "https://www.premierleague.com/en/players?competition=8&season=2026"
FPL_BOOTSTRAP = "https://fantasy.premierleague.com/api/bootstrap-static/"
OUT_JSON = "headshots.json"
OUT_UNMATCHED = "headshots_unmatched.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Matches e.g. https://resources.premierleague.com/premierleague25/photos/players/40x40/232980.png
IMG_RE = re.compile(r'<img[^>]*?src="(https://resources\.premierleague\.com/[^"]*?/photos/players/(\d+x\d+)/p?(\d+)\.png)"[^>]*?alt="([^"]*)"', re.I)
IMG_RE_ALT_FIRST = re.compile(r'<img[^>]*?alt="([^"]*)"[^>]*?src="(https://resources\.premierleague\.com/[^"]*?/photos/players/(\d+x\d+)/p?(\d+)\.png)"', re.I)
# Bigger sizes to try, largest first. Whatever the server actually serves wins.
SIZE_CANDIDATES = ["250x250", "220x280", "200x200", "150x150", "110x140", "100x100", "80x80", "40x40"]


def http_get(url, method="GET", timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, (r.read() if method == "GET" else b"")


def norm(name):
    """'Martin Ødegaard' -> 'martin odegaard'"""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ø", "o").replace("Ø", "o").replace("ß", "ss").replace("ł", "l").replace("đ", "d")
    s = re.sub(r"[^a-zA-Z\s'-]", " ", s).lower().replace("-", " ").replace("'", "")
    return re.sub(r"\s+", " ", s).strip()


def parse_headshots(html):
    """Returns {photo_id: {"url", "size", "name"}} from a chunk of HTML."""
    found = {}
    for url, size, pid, alt in IMG_RE.findall(html):
        found[pid] = {"url": url, "size": size, "name": alt.strip()}
    for alt, url, size, pid in IMG_RE_ALT_FIRST.findall(html):
        found.setdefault(pid, {"url": url, "size": size, "name": alt.strip()})
    return found


def scrape_plain():
    try:
        _, body = http_get(PLAYERS_URL)
        return parse_headshots(body.decode("utf-8", "replace"))
    except Exception as e:
        print(f"  plain fetch failed: {e}")
        return {}


NEXT_SELECTORS = [
    "button[aria-label*='next' i]:not([disabled])", "a[aria-label*='next' i]",
    "button[title*='next' i]:not([disabled])", "a[title*='next' i]",
    "[class*='pagination' i] button[class*='next' i]:not([disabled])", "[class*='pagination' i] a[class*='next' i]",
    "button[class*='next' i]:not([disabled])", "a[class*='next' i]",
    "button:has-text('Next'):not([disabled])", "a:has-text('Next')",
]
ARROW_TEXTS = ["›", "»", ">", "→", "❯"]


def page_ids(page):
    return set(parse_headshots(page.content()).keys())


def wait_for_change(page, before, timeout_ms=10000):
    """After a click, wait until the list of players on screen is different."""
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(400)
        waited += 400
        now = page_ids(page)
        if now and now != before:
            return True
    return False


# The PL site's pagination is two round icon buttons (no text) under the list.
# Mark the rightmost enabled icon button just below the last player row.
FIND_ARROW_JS = r"""() => {
  document.querySelectorAll('[data-hs-next]').forEach(e => e.removeAttribute('data-hs-next'));
  const imgs = [...document.querySelectorAll('img[src*="/photos/players/"]')];
  if (!imgs.length) return false;
  const listBottom = Math.max(...imgs.map(i => i.getBoundingClientRect().bottom + window.scrollY));
  const cands = [...document.querySelectorAll('button, a, [role="button"]')].filter(b => {
    const r = b.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const top = r.top + window.scrollY;
    if (top < listBottom - 5 || top > listBottom + 500) return false;
    if ((b.innerText || '').trim().length > 2) return false;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
    return true;
  });
  if (!cands.length) return false;
  cands.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
  cands[0].setAttribute('data-hs-next', '1');
  return true;
}"""


def click_next(page, page_num):
    """Tries the PL arrow button first, then every common kind of 'next page' control.
    Returns True once the player list on screen has actually changed."""
    before = page_ids(page)
    page.mouse.wheel(0, 20000)   # pagination sits under the list
    page.wait_for_timeout(500)
    candidates = []
    try:
        if page.evaluate(FIND_ARROW_JS):
            candidates.append(page.locator("[data-hs-next='1']").first)
    except Exception:
        pass
    candidates += [page.locator(sel).first for sel in NEXT_SELECTORS]
    for t in ARROW_TEXTS:
        candidates.append(page.locator(f"button:text-is('{t}'), a:text-is('{t}')").first)
    candidates.append(page.locator(f"button:text-is('{page_num + 1}'), a:text-is('{page_num + 1}')").first)
    for loc in candidates:
        try:
            if loc.count() and loc.is_visible(timeout=300) and loc.is_enabled(timeout=300):
                loc.click(timeout=2000)
                if wait_for_change(page, before):
                    return True
        except Exception:
            continue
    return False


def dump_pagination_debug(page):
    """Saves the HTML around the pagination so the right button can be targeted next time."""
    try:
        html = page.evaluate("""() => [...document.querySelectorAll('nav, [class*="pagination" i], [class*="pager" i], [aria-label*="pag" i]')]
            .map(e => e.outerHTML.slice(0, 3000)).join('\\n\\n-----\\n\\n')""")
        open("headshots_debug.html", "w", encoding="utf-8").write(html or page.content()[-20000:])
        print("  Saved headshots_debug.html. Send me that file and I'll target the exact button.")
    except Exception:
        pass


def scrape_with_browser(manual=False, headless=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright isn't installed. Run:  pip install playwright && playwright install chromium")
        sys.exit(1)
    found = {}
    with sync_playwright() as p:
        # A normal-looking, visible browser: the site hides its pagination from obvious automation
        browser = p.chromium.launch(headless=headless and not manual,
                                    args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1800}, locale="en-GB")
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        page = context.new_page()
        page.goto(PLAYERS_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=25000)   # the arrows only appear once loading is done
        except Exception:
            pass
        page.wait_for_timeout(2000)
        for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept All Cookies')", "button:has-text('Accept')"]:
            try:
                page.locator(sel).first.click(timeout=1500)
                break
            except Exception:
                pass
        page.wait_for_timeout(1500)

        def collect(page_num):
            new = {k: v for k, v in parse_headshots(page.content()).items() if k not in found}
            found.update(new)
            print(f"  page {page_num}: +{len(new)} (total {len(found)})")

        page_num = 1
        collect(page_num)
        if manual:
            print("\nA browser window is open. For each page: click 'next' in the browser, then press Enter here.")
            print("Type q and press Enter when you've reached the last page.\n")
            while True:
                if input(f"  Enter = save page {page_num + 1}, q = finish: ").strip().lower() == "q":
                    break
                page_num += 1
                page.wait_for_timeout(800)
                collect(page_num)
        else:
            while page_num < 200:
                if not click_next(page, page_num):
                    print(f"  No further pages found after page {page_num}.")
                    if page_num == 1:
                        dump_pagination_debug(page)
                        print("  Tip: run  python fetch_headshots.py --manual  and click through the pages yourself.")
                    break
                page_num += 1
                collect(page_num)
        browser.close()
    return found


def best_size(sample):
    """Finds the largest photo size the server actually serves, using one known headshot."""
    for size in SIZE_CANDIDATES:
        url = sample["url"].replace(f"/{sample['size']}/", f"/{size}/")
        try:
            status, _ = http_get(url, method="HEAD", timeout=10)
            if status == 200:
                return size
        except urllib.error.HTTPError:
            continue
        except Exception:
            continue
    return sample["size"]


def main():
    print("1/4 Loading the FPL player list...")
    try:
        _, body = http_get(FPL_BOOTSTRAP)
        elements = [e for e in json.loads(body)["elements"] if not e.get("removed")]
    except Exception as e:
        print(f"Couldn't load the FPL API: {e}")
        sys.exit(1)
    print(f"  {len(elements)} FPL players")

    print("2/4 Collecting headshots from premierleague.com...")
    headshots = scrape_plain()
    print(f"  plain page: {len(headshots)} headshots")
    manual = "--manual" in sys.argv
    if manual or len(headshots) < 200:
        print("  Opening a browser to go through every page..." if manual else "  The page builds its list with JavaScript, opening a browser to click through the pages...")
        headshots.update(scrape_with_browser(manual=manual, headless="--headless" in sys.argv))
    print(f"  {len(headshots)} headshots found")
    if not headshots:
        print("No headshots found. The page layout may have changed; open it in a browser and check an <img> tag.")
        sys.exit(1)

    print("3/4 Matching to FPL players...")
    by_code = {str(e["code"]): e for e in elements if e.get("code")}
    by_photo = {str(e.get("photo", "")).split(".")[0]: e for e in elements if e.get("photo")}
    full_names, web_names = {}, {}
    for e in elements:
        full_names.setdefault(norm(f"{e['first_name']} {e['second_name']}"), []).append(e)
        web_names.setdefault(norm(e["web_name"]), []).append(e)

    matched, via_id, via_name, unmatched = {}, 0, 0, []
    for pid, h in headshots.items():
        e = by_code.get(pid) or by_photo.get(pid)
        if e:
            via_id += 1
        else:
            n = norm(h["name"])
            cands = full_names.get(n) or []
            if len(cands) != 1:
                cands = web_names.get(n) or []
            if len(cands) != 1:  # try "first last" against FPL's (sometimes longer) full names
                toks = n.split()
                if len(toks) >= 2:
                    cands = [x for x in elements if norm(x["second_name"]).endswith(toks[-1]) and norm(x["first_name"]).startswith(toks[0])]
            if len(cands) == 1:
                e = cands[0]
                via_name += 1
        if e:
            matched[str(e["id"])] = h
        else:
            unmatched.append(f"{h['name']}  ({h['url']})")

    print("4/4 Picking the best image size...")
    sample = next(iter(headshots.values()))
    size = best_size(sample)
    print(f"  using {size}")
    players = {fid: h["url"].replace(f"/{h['size']}/", f"/{size}/") for fid, h in matched.items()}

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(), "source": PLAYERS_URL, "size": size,
        "matchedById": via_id, "matchedByName": via_name, "unmatched": len(unmatched),
        "idsMatchFplCodes": via_id >= 0.9 * max(1, len(headshots)),
        "urlTemplate": sample["url"].replace(f"/{sample['size']}/", f"/{size}/").rsplit("/", 1)[0] + "/{code}.png",
        "players": players,
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    open(OUT_UNMATCHED, "w", encoding="utf-8").write("\n".join(sorted(unmatched)) + "\n")

    print(f"\nDone: {len(players)} of {len(elements)} FPL players have a headshot -> {OUT_JSON}")
    print(f"  matched by ID: {via_id}, by name: {via_name}, unmatched: {len(unmatched)} (see {OUT_UNMATCHED})")
    if via_id and via_id >= 0.75 * (via_id + via_name + len(unmatched)):
        print("  Good news: photo IDs are FPL player codes, so the app can build headshot URLs straight from FPL data.")


if __name__ == "__main__":
    main()
