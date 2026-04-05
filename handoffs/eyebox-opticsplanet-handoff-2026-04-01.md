# Eyebox handoff: OpticsPlanet live-page extraction decision

This is the single handoff payload for the Eyebox agent.

## Executive conclusion

We now have enough evidence to stop treating live OpticsPlanet as primarily a browser-access / anti-bot problem.

Current best judgment:
- browser-backed access to the live category page works
- the current OpticsPlanet extraction failure is primarily selector mismatch
- a small extra wait does not materially change the result
- the page exposes a stronger structured data path via `productListConfig.data.gridProducts.elements`

So the next narrow Eyebox step should be:
1. stop using the old broad item selector assumptions
2. prefer structured extraction from `productListConfig.data.gridProducts.elements`
3. if DOM extraction is still desired, switch to the real product tile container and data-bearing attrs

## What was verified

### Live URL
- `https://www.opticsplanet.com/red-dot-sights-and-accessories.html`

### Direct HTTP
- still blocked
- status: 403
- title: `Access Blocked`

### Browser-backed dynamic run with minimal title selector
Verified via Scrapeyard service path:
- final URL: `https://www.opticsplanet.com/red-dot-sights-and-accessories.html`
- page title: `Red Dot Sights & Accessories for Sale Up to 76% Off`
- main document status: 200
- classification: successful browser-backed page load

Meaning:
- live browser-backed access works
- this is not primarily “cannot reach the page in-browser”

### Browser-backed dynamic run with current broad selectors
Verified via Scrapeyard service path after adding observability:
- intended page reached
- item selector count: 1
- field selector counts relative to matched item:
  - `name: 0`
  - `price: 0`
  - `url: 0`
  - `image_url: 0`
- classification: `selector_miss`

Meaning:
- the current selector package is looking at the wrong container / wrong field path
- this is not well explained by anti-bot alone

### Browser-backed dynamic run with `wait_ms: 1500`
- outcome stayed effectively the same
- classification stayed `selector_miss`

Meaning:
- small wait/hydration mismatch is not the main blocker

## What the real live DOM looks like

### The old item selector is wrong
Observed candidate counts on the live page:
- `.product-card`: 0
- `.item`: 1
- `.product-item`: 0
- `[data-sku]`: 69
- `.product`: 97

Important finding:
- `.item` is matching the page header block, not a product tile

That explains why the old broad selector strategy can produce one matched container but zero useful field matches.

### Real product tile/container pattern
Observed live product tile example:
- tag: `DIV`
- class: `grid gtmProduct two-block-tall product product_w-models float-left js-carousel-item`

Observed container attributes:
- `data-name`
- `data-url`
- `data-price`
- `data-id`
- `data-brand`

Example values from a real tile:
- `data-name`: `Holosun HS507COMP 1x1.1x0.87in Reflex Red Dot Sight`
- `data-url`: `holosun-he507comp-open-reflex-optical-sight`
- `data-price`: `369.99`
- anchor href: `https://www.opticsplanet.com/holosun-he507comp-open-reflex-optical-sight.html`

### Better DOM selector candidates
If Eyebox stays DOM-based first, use one of these container candidates instead:
- `[data-url][data-name][data-price]`
- `.gtmProduct`
- `.grid.gtmProduct`
- `.product.js-carousel-item`

Candidate field paths:
- name:
  - `data-name`
  - fallback: anchor `title`
- url:
  - `a.grid__link::attr(href)`
  - or build from `data-url` + `.html`
- price:
  - `data-price`
- image_url:
  - inspect lazy-image attrs before trusting `img[src]`; observed `img[src]` can be fallback image

## Stronger alternate data path
A large inline page script exists:
- `var productListConfig = {...}`

This was successfully parsed from the live page.

Observed facts:
- `productListConfig.data.total = 2284`
- `productListConfig.data.filtered = 2284`
- `productListConfig.data.gridProducts.elements` contains product objects

Observed fields in the parsed product objects include:
- `url`
- `anchorText`
- `fullName`
- `imageUrl`
- `price`
- `listPrice`
- `primaryImage`
- `productCode`
- `reviewCount`
- `reviewRating`
- `sku`
- `variantCount`
- `brandName`
- `categoryName`
- `stockAvailabilityMessage`

This is the highest-value extraction path currently observed.

## Recommendation to Eyebox agent

### Preferred next step
Implement / test extraction from:
- `productListConfig.data.gridProducts.elements`

Reason:
- it is already product-structured
- it appears more stable than brittle DOM selectors
- it already exposes most of the fields Eyebox needs

### Secondary option
If structured data parsing is not immediately available, rewrite the OpticsPlanet DOM selectors around the real container:
- item selector: `.gtmProduct[data-url][data-name][data-price]`
- then read name/price/url from the container attrs / real anchor

## What not to do next
- do not start with proxy/session identity work
- do not conclude anti-bot is the primary blocker for browser-backed runs
- do not keep the old broad selector package as-is
- do not over-index on adding small waits first

## Final judgment

Primary blocker:
- selector mismatch

Strong opportunity:
- alternate structured data-path extraction via `productListConfig`

Not supported as the primary blocker by current evidence:
- browser-access failure
- small hydration/wait mismatch
- consent/challenge interstitial on successful browser-backed live page load
