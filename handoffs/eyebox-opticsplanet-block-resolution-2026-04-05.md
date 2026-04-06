# Eyebox Handoff: OpticsPlanet Browser Block — Root Cause & Resolution

## TL;DR

OpticsPlanet is blocking Scrapeyard's `dynamic` fetcher because it runs **stock Playwright Chromium with zero stealth** (`stealth=False` default). The browser is trivially detectable by any decent bot-detection system.

**The fix: change `fetcher: dynamic` to `fetcher: stealthy` in the OpticsPlanet config.**

No Scrapeyard code change is needed. The `stealthy` fetcher (Camoufox / modified Firefox) is already fully wired and operational.

---

## Root cause analysis

### What `dynamic` actually does

`dynamic` maps to Scrapling's `PlayWrightFetcher`, which launches Chromium via Playwright.

Scrapeyard's `_fetch_page()` passes these kwargs to it:
- `timeout`
- `disable_resources`
- `network_idle`
- `wait_selector` (if configured)
- `wait` (if configured)
- `page_action` (debug capture / click handling)
- `proxy` (if configured)

Critically, Scrapeyard does **NOT** pass:
- `stealth` → defaults to `False`
- `hide_canvas` → defaults to `False`
- `useragent` → defaults to auto-generated
- `extra_headers` → defaults to None

When `stealth=False`, PlayWrightFetcher:
1. Uses stock `playwright` (not `rebrowser_playwright`)
2. Injects zero stealth launch flags
3. Injects zero stealth JS scripts (no webdriver bypass, no CDP fingerprint patch)
4. Leaves `navigator.webdriver = true` (trivially detectable)
5. Exposes the CDP runtime fingerprint
6. Applies no canvas noise, no automation hiding

This is **stock headless Chromium** — the easiest browser automation to detect.

### What `stealthy` does instead

`stealthy` maps to Scrapling's `StealthyFetcher`, which launches **Camoufox** — a purpose-built modified Firefox binary designed for anti-detection:

- Modified Firefox engine (fundamentally different fingerprint surface than Chromium)
- `humanize=True` by default (simulated cursor movement)
- OS-matched fingerprints via browserforge
- WebGL enabled (many WAFs now check for this)
- Google search referer by default
- WebRTC configurable
- GeoIP support for proxy locale matching
- Passes major bot detection suites (bot.sannysoft.com, browserscan.net, iphey.com, etc.)

Scrapeyard already passes all necessary kwargs to `StealthyFetcher.async_fetch()` correctly: timeout, disable_resources, network_idle, wait_selector, wait, page_action, proxy, custom_config. **No code changes needed.**

### Why this changed since April 1

The April 1 handoff correctly observed that `dynamic` fetcher reached the real page at that time. Since then, OpticsPlanet appears to have tightened their bot detection. Stock Playwright Chromium — which was marginal before — now gets caught and redirected to `forbidden.html`.

This is a common pattern: sites progressively tighten anti-bot, and automation that barely worked before starts failing.

### Why plain HTTP still works

The `basic` fetcher (httpx) uses `stealthy_headers=True`, which generates realistic Chrome/Firefox/Edge headers via browserforge and sets a Google search referer. It has **zero browser fingerprint surface** — there's no JavaScript execution, no WebGL, no canvas, no CDP, no webdriver property. OpticsPlanet's bot detection focuses on browser-level signals, not simple HTTP header analysis.

---

## The fix

### Config change required

In the OpticsPlanet scrape config YAML:

```yaml
# BEFORE (blocked):
fetcher: dynamic

# AFTER (should work):
fetcher: stealthy
```

Apply this to all OpticsPlanet targets.

### Also update the stale comment

The brief notes this comment exists at the top of `configs/opticsplanet-optics.yaml`:

> `Live browser-backed access was verified; current blocker is selector correctness, not primary access failure.`

Replace it with something like:

```yaml
# Browser access: requires `stealthy` fetcher. `dynamic` (Playwright Chromium)
# gets blocked by OpticsPlanet bot detection as of 2026-04-05.
# See: handoffs/eyebox-opticsplanet-block-resolution-2026-04-05.md
```

### BrowserConfig settings to review

With `stealthy`, the `browser:` block in the YAML still applies. Relevant settings:

- `timeout_ms`: still respected (default 60000)
- `disable_resources`: still respected (but be careful — setting `true` can break some sites with Camoufox)
- `network_idle`: still respected
- `wait_for_selector`: still respected
- `wait_ms`: still respected
- `click_selector`: still respected (for consent gates)

**Recommendation:** If `disable_resources: true` is currently set for OpticsPlanet, consider setting it to `false` with the stealthy fetcher. Resource blocking can interfere with page loading on some sites and Camoufox already provides speed improvements through its own optimizations.

---

## What about the selector work from April 1?

The April 1 handoff identified strong selector candidates and the `productListConfig` structured data path. **That work is still valid and still needed.** Switching to `stealthy` fixes the *access* problem; the selector work fixes the *extraction* problem. Both are needed for OpticsPlanet to work end-to-end.

Recommended extraction approach (unchanged from April 1):
1. Preferred: parse `productListConfig.data.gridProducts.elements` from inline script
2. Fallback DOM: item selector `.gtmProduct[data-url][data-name][data-price]` with `data-name`, `data-price`, `data-url` attribute selectors

---

## Verification steps

After making the config change, verify with a single OpticsPlanet URL:

**URL:** `https://www.opticsplanet.com/red-dot-sights-and-accessories.html`

**Check these signals:**
- `final_url` should stay on the category page (NOT redirect to `forbidden.html`)
- `page_title` should be the real category title (NOT "Before we continue")
- `item_selector_count` should be > 0 (with correct selectors)
- `classification` should NOT be `blocked_response`
- `gtmProduct` should be present in page HTML
- `data-sku` should be present in page HTML

**If stealthy still gets blocked:**
That would mean OpticsPlanet is blocking at a deeper level (IP reputation, rate limiting, or advanced Camoufox detection). In that case, escalate back to Scrapeyard for:
- Proxy/session identity investigation
- Cookie persistence / session warming
- GeoIP proxy alignment (Camoufox supports `geoip=True`)

---

## What Scrapeyard will improve separately (not blocking this fix)

As a separate improvement track, Scrapeyard may add stealth-related fields to `BrowserConfig` so that `dynamic` fetcher users can opt into stealth mode per-config:

- `stealth: true/false` — enable PW stealth mode
- `hide_canvas: true/false` — canvas fingerprint noise
- `extra_headers: {}` — additional HTTP headers
- `useragent: string` — custom user-agent override

This would make `dynamic` more useful against sites with moderate bot detection. But for sites with strong detection like OpticsPlanet, `stealthy` (Camoufox) remains the recommended fetcher.

---

## Summary

| Question | Answer |
|----------|--------|
| Why is dynamic blocked? | Stock Playwright Chromium, stealth=False, trivially detectable |
| Why does plain HTTP work? | No browser fingerprint surface, just good HTTP headers |
| What's the fix? | `fetcher: stealthy` (Camoufox) in OpticsPlanet config |
| Does Scrapeyard need a code change? | No — stealthy fetcher path is already fully wired |
| Is the April 1 selector work still needed? | Yes — access fix and selector fix are both required |
| What if stealthy also fails? | Escalate back for proxy/session/geoip investigation |
