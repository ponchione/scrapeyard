# Bass Pro / Cabela's Scrapeyard access readout for Eyebox agent

## Requested bottom-line format

1. `Can current Scrapeyard scrape Bass Pro/Cabela's from our local setup today? no`
2. `Is the main blocker IP reputation, fingerprinting, missing browser runtime, missing proxy, or something else?`
   Main blocker: anti-bot access from this environment, likely a combination of local IP reputation plus browser/session identity.
   Secondary blocker: dynamic-with-stealth is locally broken because the image installs Playwright Chromium, but not the rebrowser Chromium revision that Scrapling uses when dynamic `browser.stealth: true`.
3. `What single next change has the highest chance of getting a real PLP response?`
   Add one good residential proxy to a minimal `stealthy` liveness probe and collect artifacts.
4. `What exact command/config patch should we run next to test that hypothesis?`
   Use a one-page proxy-backed `stealthy` title-only probe first.

---

## Exact next probe to run

YAML:

```yaml
project: eyebox
name: basspro-red-dot-stealthy-proxy-probe
adaptive: false
proxy:
  url: "http://USER:PASS@RESIDENTIAL_GATE:PORT"
target:
  url: "https://www.basspro.com/l/red-dot"
  fetcher: stealthy
  browser:
    timeout_ms: 60000
    disable_resources: false
    network_idle: true
    wait_for_selector: "body"
    wait_ms: 1500
    extra_headers:
      Accept-Language: "en-US,en;q=0.9"
      Upgrade-Insecure-Requests: "1"
  selectors:
    title: "title::text"
validation:
  required_fields: [title]
  min_results: 1
  on_empty: warn
execution:
  mode: async
```

Command:

```bash
curl -sS -X POST http://localhost:8420/scrape \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @basspro-proxy-probe.yaml
```

Recommended immediate follow-up if Bass Pro improves:
- run the same probe against `https://www.cabelas.com/l/red-dot`

---

## Grounded findings

### Live local service state

- Scrapeyard health is up at `http://localhost:8420/health`
- Current health response showed:
  - `status: ok`
  - `workers.max_concurrent: 4`
  - `workers.max_browsers: 2`
  - `active_browsers: 0`

### Direct requests from this machine are blocked now

Verified live from this host:
- `https://www.basspro.com/l/red-dot` -> HTTP 403 Access Denied
- `https://www.cabelas.com/l/red-dot` -> HTTP 403 Access Denied

This matches the earlier user-provided evidence.

### Local Scrapeyard evidence from recorded jobs

#### Bass Pro dynamic probe
Job: `8367ff6d-fc33-4a88-9273-54eea85e3739`

Result:
- failed
- `error_type: blocked_response`
- `main_document_status: 403`
- `page_title: Access Denied`
- `classification: blocked_response`

Key debug excerpt:
- `FetchError: HTTP 403`
- HTML excerpt was the standard Access Denied page

Interpretation:
- stock dynamic from this environment is blocked hard

#### Bass Pro stealthy probe
Job: `cc256ad3-0e91-4540-ab82-18cfcd1664f8`

Result:
- overall failed via validation warning path
- target fetch itself returned 200
- `main_document_status: 200`
- `item_selector_count: 0`
- `classification: rendered_empty`

Key debug excerpt contained:
- `sec-if-cpt-container`
- `Powered and protected by Akamai`
- `akam-sw.js install script`
- service-worker bootstrap logic

Interpretation:
- this was not the real PLP
- it was an Akamai interstitial/bootstrap page, not a selector miss on a real product grid

#### Bass Pro hardened dynamic probe
Job: `86d5b987-4bff-47a7-a4ca-cf57835604e5`

Result:
- failed
- `error_type: browser_error`

Key error:
- `BrowserType.launch: Executable doesn't exist at /root/.cache/ms-playwright/chromium-1169/chrome-linux/chrome`

Interpretation:
- local dynamic-with-stealth runtime is broken
- this is not evidence that Bass Pro is accessible; it is a local runtime gap

#### Bass Pro hardened stealthy probe
Job: `6618904b-7f27-443d-97f9-d335741ac72a`

Result:
- job marked complete, but only because it emitted a null title row and validation warned instead of failing hard
- `main_document_status: 200`
- selector count for title was 0
- HTML excerpt again contained Akamai interstitial/service-worker bootstrap markers
- classification remained `rendered_empty`

Interpretation:
- still not a real PLP response
- still an access problem, not extraction correctness

#### Cabela's control liveness probe
Job: `8ff5b4a7-ea99-4fe9-b5b3-a7018696d1fb`

Result:
- failed
- `error_type: blocked_response`
- `main_document_status: 403`
- `page_title: Access Denied`

Interpretation:
- this appears broader than Bass Pro alone
- same environment is blocked against related storefront family

---

## Direct answers to the original questions

### A. Bass Pro / Cabela's access diagnosis

1. Does this look like hard local IP reputation block, fingerprint block, missing-session/service-worker problem, or a combination?

Best current read: combination.

Why:
- direct host requests get hard 403, which strongly suggests local network/IP reputation or environment-level access denial
- dynamic without stealth gets hard 403 too
- stealthy sometimes gets a 200 but only lands on the Akamai interstitial/service-worker bootstrap page

That means:
- not just selectors
- not just one broken fetcher
- not just one missing wait
- likely network identity plus browser/session identity

2. Have we seen Bass Pro or Cabela's work recently in Scrapeyard from another environment or runtime?

No grounded evidence found in recent session recall or repo docs that Bass Pro/Cabela's are currently working somewhere else.

3. Are Bass Pro/Cabela's unsupported globally by Scrapeyard, or only from our present local environment?

Grounded answer:
- unsupported from our present local environment: yes
- globally unsupported by Scrapeyard as a product: not proven

### B. Dynamic / Playwright runtime

4. Why is the current Scrapeyard container missing Playwright Chromium? Is that expected or drift?

More precise answer:
- the image does install Playwright Chromium
- but the hardened dynamic probe used `browser.stealth: true`
- Scrapling `PlayWrightFetcher` switches to `rebrowser_playwright` when `stealth=True` and `real_chrome=False`
- the image currently has Playwright browser revision `1208`
- `rebrowser_playwright` expects Chromium revision `1169`
- that `1169` browser is not installed

So the issue is not simply "Playwright missing". It is:
- rebrowser Chromium required by dynamic-stealth is missing from the container

5. What exact command/image rebuild should restore working dynamic Playwright probes locally?

Fastest in-place fix:

```bash
docker compose exec scrapeyard rebrowser_playwright install chromium
```

Safer recreate path:

```bash
docker compose up -d --build --force-recreate scrapeyard
docker compose exec scrapeyard rebrowser_playwright install chromium
```

6. Once fixed, do we expect dynamic to be materially better than stealthy?

Not as the first bet.

Evidence so far:
- dynamic without stealth got hard 403
- stealthy without proxy got only the Akamai interstitial

So even after fixing rebrowser, the highest-value next change is still better network identity via residential proxy.

### C. Hidden capability surface

7. Are there existing Scrapeyard-supported ways to pass through more of Scrapling's anti-bot knobs that we are not using yet?

No, not beyond the currently exposed config surface.

Current Scrapeyard-exposed browser config includes:
- `timeout_ms`
- `disable_resources`
- `network_idle`
- `stealth`
- `hide_canvas`
- `useragent`
- `extra_headers`
- `click_selector`
- `click_timeout_ms`
- `click_wait_ms`
- `wait_for_selector`
- `wait_ms`

8. Is there already a supported path for these in current Scrapeyard?
- `real_chrome`: no
- `cdp_url`: no
- `nstbrowser_mode`: no
- `humanize`: no
- `os_randomize`: no
- `geoip`: no
- `disable_ads`: no
- `additional_arguments`: no

These exist in installed Scrapling fetcher signatures, but Scrapeyard does not currently expose/pass them through.

9. If not exposed today, is there a recommended patch pattern for narrow one-off probes?

Yes. Narrow patch pattern:
- add optional fields to `BrowserConfig` in `src/scrapeyard/config/schema.py`
- map them in `src/scrapeyard/engine/browser_debug.py`
- add them to `default_debug_blob()` so debug output records what was used
- add focused tests in:
  - `tests/unit/test_scraper_decomposition.py`
  - `tests/unit/test_scraper_adaptive.py`

### D. Proxy/session requirements

10. For Bass Pro/Cabela's, is one good residential proxy enough for validation, or is true rotation/session affinity needed?

For the next proof run, one good sticky residential proxy is enough.

Rotation/session affinity may matter later for production reliability, but not for the first binary access proof.

11. Does Scrapeyard currently preserve cookies/service-worker/session state in a way that matters for Akamai sites?

No meaningful hostile-site session strategy is present at the Scrapeyard layer.

What exists:
- per-fetch browser context/page execution
- optional waits
- optional click via `page_action`
- adaptive matching database, but that is for selector resilience, not hostile session persistence

What does not exist:
- sticky long-lived session profiles
- explicit cookie jar reuse policy for anti-bot flows
- service-worker-aware persistence strategy
- hostile-retailer session warming flow

So each fetch is effectively too stateless to claim robust Akamai handling.

12. If we add a proxy today, what is the minimum config/runtime needed to test whether proxy alone unlocks the real PLP?

Minimum worthwhile test:
- `fetcher: stealthy`
- one residential proxy URL
- `disable_resources: false`
- `network_idle: true`
- `wait_for_selector: body`
- `wait_ms: 1500`
- simple selector `title: title::text`
- collect debug artifacts from result object

### E. Interstitial handling

13. Does current Scrapeyard have a mechanism to wait through or interact past that Akamai interstitial?

No Akamai-specific mechanism exists.

Current behavior is effectively:
- fetch page
- optional click
- optional selector wait
- optional fixed wait
- capture debug/screenshot
- extract
- stop

14. Would `click_selector` realistically help here?

Probably not.

`click_selector` is appropriate for consent/age-gate style DOM interactions. The observed Bass Pro interstitial looks like an Akamai challenge/bootstrap flow, not a simple click-through modal.

15. Is there a known Scrapeyard/Scrapling way to let Akamai service-worker/bootstrap complete beyond `network_idle` and `wait_ms`?

Not in current Scrapeyard config surface.

Possible future extension would be custom `page_action` logic or exposing more underlying knobs, but there is no existing Akamai-aware flow in Scrapeyard today.

### F. What would actually prove Bass Pro is scrapeable

16. What is the narrowest proof run next?

Best first proof:
- one-page Bass Pro red-dot title-only liveness probe through a residential proxy using `stealthy`

Best follow-up control:
- same probe against Cabela's through the same proxy

17. What exact artifacts should we collect?

Current Scrapeyard already captures most of these:
- screenshot
- HTML excerpt
- final URL
- response status / main document status
- page title
- browser settings used

If access is still ambiguous, add more observability next:
- browser console messages
- requestfailed / network errors
- maybe current cookies and redirect chain summaries

18. If picking one minimal change with highest expected value, what comes first?

First choice:
- add residential proxy

Second choice:
- repair dynamic-stealth runtime by installing rebrowser Chromium

Only after that:
- expose hidden Camoufox knobs or real Chrome/CDP mode

---

## Capability surface readout from current Scrapeyard code

### What Scrapeyard supports today

Proxy support:
- yes, but only single resolved URL
- precedence: target proxy > job proxy > service proxy
- no proxy pool management
- no rotation

Browser tuning exposed in config:
- `timeout_ms`
- `disable_resources`
- `network_idle`
- `stealth`
- `hide_canvas`
- `useragent`
- `extra_headers`
- `click_selector`
- `click_timeout_ms`
- `click_wait_ms`
- `wait_for_selector`
- `wait_ms`

Browser debug capture includes:
- `final_url`
- `page_title`
- `main_document_status`
- `html_excerpt`
- `screenshot_path`
- browser settings used

### What Scrapeyard does not support today

- proxy rotation / proxy pool management
- hostile-site session persistence strategy
- Akamai-specific challenge handling
- built-in challenge solver / consent solver beyond optional click selector
- browser console capture in stored results
- requestfailed/network diagnostics in stored results
- pass-through for these underlying Scrapling knobs:
  - `real_chrome`
  - `cdp_url`
  - `nstbrowser_mode`
  - `humanize`
  - `os_randomize`
  - `geoip`
  - `disable_ads`
  - `additional_arguments`

### Important nuance on adaptive

`adaptive` in Scrapeyard is for Scrapling adaptive matching / selector resilience.
It is not evidence of anti-bot capability.

---

## Why the dynamic-stealth runtime failed locally

Grounded explanation:
- Dockerfile installs stock Playwright Chromium
- Dockerfile does not install rebrowser Chromium explicitly
- Scrapling `PlayWrightFetcher` uses stock `playwright` when `stealth=False`
- Scrapling `PlayWrightFetcher` uses `rebrowser_playwright` when `stealth=True` and `real_chrome=False`
- installed `rebrowser_playwright` expects Chromium revision `1169`
- container only had Playwright Chromium revision `1208`

Therefore:
- plain dynamic may run
- dynamic with stealth needs rebrowser Chromium 1169 and currently fails

Suggested local repair command:

```bash
docker compose exec scrapeyard rebrowser_playwright install chromium
```

---

## Minimal recommendation to Eyebox agent

Decision-first summary:
- do not spend time on Bass Pro/Cabela's selectors yet
- do not treat the current 200 stealthy result as page access success
- treat current issue as hostile-site access failure from local environment
- highest-value next proof is a residential-proxy-backed `stealthy` liveness probe
- separately fix dynamic-stealth runtime if you want to compare fetchers afterward

Recommended sequencing:
1. run one Bass Pro red-dot proxy-backed stealthy title-only probe
2. inspect final URL, title, html excerpt, screenshot, status
3. if real PLP appears, run the same probe against Cabela's as control
4. only if proxy succeeds but stability is still poor, consider exposing hidden Camoufox knobs
5. only if needed after that, compare against repaired dynamic-stealth or real Chrome / CDP mode

---

## If Eyebox wants one sentence to carry forward

Current Scrapeyard cannot reliably access Bass Pro/Cabela's from our local setup today; the dominant issue is anti-bot access from this environment, and the single highest-value next test is a residential-proxy-backed `stealthy` liveness probe, not selector work.
