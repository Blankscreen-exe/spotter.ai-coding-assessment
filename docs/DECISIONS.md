# Decision log

A running record of how this project was built: what we found, what options were
on the table, what was chosen and why. Entries were written as the decisions were
made, with the numbers that were in front of us at the time.

**How the work was split.** I (the candidate) made the design decisions and
reviewed the result; Claude Code (an AI coding assistant) did the investigation,
laid out options with trade-offs and measurements, and wrote the implementation.
Each entry says who made the call. "Claude default" means the assistant picked a
sensible option without asking and I left it in place.

## 1. Reading the brief

The assignment is in [test_prompt.md](test_prompt.md). The constraints that drive
the design:

| Constraint | Consequence |
| --- | --- |
| Start and finish are both in the USA | Only US fuel stations matter |
| 500 mile maximum range | Long routes need several stops; a station must exist within every 500 mile stretch |
| 10 miles per gallon | Fuel cost = gallons bought x price; the tank holds 50 gallons |
| "Optimal mostly means cost effective" | Stop selection is a cost minimisation, not "nearest station" |
| One routing API call is ideal, two or three acceptable | Nothing per-station can hit an external API at request time |
| "The quicker the better" | Everything except the route call has to be local and precomputed |
| Latest stable Django | Django 6.1, which needs a newer Python than the machine default |

## 2. What is in the fuel price file

Profiled `fuel-prices-for-be-assessment.csv` before designing anything.

| Finding | Number | Why it matters |
| --- | --- | --- |
| Data rows | 8,151 | |
| Columns | ID, name, address, city, state, rack ID, retail price | **No latitude/longitude** |
| Unique truckstop IDs | 6,738 | 678 IDs appear on more than one row |
| Duplicate IDs whose rows disagree on price | 597 | Need a rule for which price to use |
| Duplicate IDs whose rows disagree on city/state | 0 | Duplicates are the same physical stop |
| Unique city/state pairs | 3,893 | Size of the geocoding job if done per town |
| "States" | 57 | Includes 9 Canadian provinces, 620 rows |
| Addresses like "I-44, EXIT 283 & US-69" | 4,431 | Highway-exit descriptions; geocoders cannot resolve most of them |
| City values with stray whitespace | 1,256 | Must be trimmed before matching |
| Price range | 2.687 to 6.399, median 3.432 | Wide enough that stop choice changes the bill |

The missing coordinates are the central problem: to know which stations lie along
a route every station needs a position, and the brief rules out looking them up
at request time.

## 3. Decisions

### 3.1 Python 3.14 and Django 6.1

- **Decided by:** the brief; Claude checked.
- The machine's default `python` is 3.10, where pip can only install Django 5.2.
  Latest stable is 6.1.2, so the project uses a virtualenv built from Python 3.14.

### 3.2 Routing: OSRM and OpenRouteService, switchable at runtime

- **Decided by:** me. Claude offered three options (OSRM, OpenRouteService, or
  OpenRouteService for geocoding too) and recommended OSRM alone. I asked for
  both, selectable from a settings table in the database, with the API key in a
  separate table and encrypted at rest.
- **What that became:**
  - `Setting`: one row per runtime setting (`routing.provider`,
    `stations.corridor_miles`, `vehicle.range_miles`, `vehicle.mpg`,
    `stops.cost_per_stop`). Defaults live in code, so an empty table still works.
    Editable in the Django admin; a request can also name a provider.
  - `ProviderCredential`: the API key, Fernet-encrypted by a custom model field.
    The encryption key comes from the environment and several can be listed, so
    it can be rotated. The admin form is write-only.
- **The trade-off I accepted:** the encrypted key still depends on one
  environment secret. What the table buys is that the provider can be switched
  and the key replaced from the admin without a deploy.
- **Status of OpenRouteService:** written to their documentation and covered by
  tests with mocked responses. It has not yet been run against the real API; see
  section 6.

### 3.3 Start and finish are resolved offline

- **Decided by:** follows from 3.2 (both options I combined used local
  geocoding); the details are Claude's.
- A request's "City, ST" is looked up in a table of 32,058 US places loaded from
  the Census Bureau Gazetteer (public domain). That keeps the request at exactly
  one external call. "lat,lon" is accepted too.
- The lookup tolerates "chicago il", "Chicago, Illinois", "Saint Louis" for
  "St. Louis", "New York City", and consolidated names ("Nashville" for
  "Nashville-Davidson"). An ambiguous name without a state gets a 400 that lists
  the candidates.

### 3.4 Station coordinates: Census first, Nominatim for the rest

- **Decided by:** me, from three options. Claude recommended Census only; I
  chose the hybrid for coverage.
- **Result:** all 6,626 US stations are located. 6,313 by Census town centroid,
  313 by Nominatim (OpenStreetMap).
- Nominatim was queried once, at one request per second, for the 242 towns the
  Census file did not list. The answers are committed in
  `data/nominatim_cache.json`, so the import runs offline and gives the same
  result every time. As a sanity check, every Nominatim position is within 25
  miles of a Census place in the same state.
- **Problems found in the data along the way** (each one was costing stations):
  accented names ("Cañon City") were being mangled by the name normalisation; a few
  Census entries are filed as "Town of Pecos"; the fuel file writes "Mc Lean"
  where the Census writes "McLean".
- **Accepted limitation:** a station sits at the centre of its town, not at its
  exit. That is typically a few miles off, which is small against a 500 mile
  range but means `miles_off_route` in the response is approximate.

### 3.5 Canadian stations are dropped

- **Decided by:** Claude default.
- The brief limits trips to the USA, so the 620 Canadian rows are excluded at
  import. A few US-to-US routes are shortest through Ontario; those stations
  will not be offered.

### 3.6 Duplicate IDs take the mean price

- **Decided by:** me, on Claude's recommendation (options: mean, lowest, highest).
- 597 truck stops are listed more than once with different prices and nothing
  says which row is right. The mean does not flatter the total; the lowest would
  understate what a driver pays.

### 3.7 The vehicle starts with a full tank, overridable per request

- **Decided by:** me, on Claude's recommendation (options: full and
  overridable, always full, start empty).
- "Total money spent" therefore means fuel bought on the way. A trip under 500
  miles costs $0 with no stops, which is correct under this model but worth
  knowing. `initial_range_miles` in the request starts the vehicle with less.
- "Start empty" was rejected because it needs a fudge: the vehicle has to be
  given just enough fuel to reach its first station.

### 3.8 Choosing the stops: cheapest fuel, then a cost per stop

This took two rounds.

- **Round 1, decided by me:** the optimal look-ahead greedy (over "cheapest
  station in each window", which is not optimal, and a general dynamic
  programme, which is heavier for the same answer). At each station: if a
  cheaper one is within range, buy just enough to reach it; otherwise fill up
  and go to the cheapest in range. This provably minimises the fuel bill.
- **What running it showed:** the cheapest plan is not a plan anyone would
  drive. Seattle to Miami came out as 20 stops, one of them for 0.13 gallons
  ($0.38), because chasing every slightly cheaper pump costs nothing in the model.
- **Round 2, decided by me from three options:** minimise fuel bill plus a fixed
  cost per stop (default $5, a setting, 0 restores the pure cheapest plan). The
  alternatives were to leave it, or to filter out small purchases afterwards,
  which would no longer be optimal for any stated objective.

Measured on live OSRM routes before choosing (fuel bill, and the increase over
the cheapest possible):

| Route | Cheapest possible | $1 per stop | $5 per stop | $10 per stop |
| --- | --- | --- | --- | --- |
| Seattle to Miami, 3,303 mi | 20 stops, $849.27 | 10 stops, +$1.64 | 8 stops, +$3.69 | 7 stops, +$11.97 |
| New York to Los Angeles, 2,810 mi | 15 stops, $708.06 | 8 stops, +$1.52 | 7 stops, +$3.19 | 6 stops, +$8.41 |
| Boston to Denver, 1,982 mi | 9 stops, $446.73 | 5 stops, +$0.45 | 4 stops, +$3.92 | 4 stops, +$3.92 |
| Chicago to Houston, 1,083 mi | 5 stops, $169.18 | 2 stops, +$0.85 | 2 stops, +$0.85 | 2 stops, +$0.85 |

(Taken before the Nominatim-located stations were added, so today's totals differ
by a dollar or two.)

- **Implementation.** A stop cost needs a dynamic programme over (station, fuel
  on arrival). It stays small because an optimal plan only ever arrives at a
  stop empty or with a full tank minus the distance from the previous stop. The
  first prototype took 50 to 350 ms on long routes; two observations about which
  arrival states can ever win brought it to about 30 ms (they are written up in
  the docstring in `planner/services/optimizer.py`).
- **Both solvers are kept.** The greedy runs when the stop cost is 0 (0.2 ms)
  and doubles as a check on the dynamic programme.
- **How I know they are right:** the greedy matches an independent formula on
  300 random routes; the dynamic programme matches brute force over every subset
  of stations on 400 small random routes; with a negligible stop cost the two
  agree on 200 larger ones.

### 3.9 Which stations count as "on the route"

- **Decided by:** Claude default.
- A station counts if it is within 5 miles of the route line (a setting). The
  detour to reach it is not added to the trip. Five miles is deliberately
  forgiving because station positions are town centres (3.4).
- Matching is done in memory with numpy, not in SQL. PostGIS would be the
  database-native way to do this and I would move to it with much more data,
  but it needs GDAL installed to run the project at all, and 6,626 stations fit
  comfortably in memory. Measured on a cross-country route: comparing every
  station to every route point took 117 ms; a coarse grid that first narrows
  6,626 stations to the few hundred near the route brought it to about 12 ms.

### 3.10 What "return a map" means

- **Decided by:** me, on Claude's recommendation.
- The API returns JSON with the route as GeoJSON, which any map client can draw.
  `/map/` renders the same plan on an interactive Leaflet map, for a person.

### 3.11 What changed after reading the job description

The job description arrived after the first working version. Three things in it
were not reflected in what we had, all of them things we had never explicitly
decided:

| The job description says | What we had | What I decided |
| --- | --- | --- |
| "we use PostgreSQL" | SQLite, by default | PostgreSQL and Redis through `docker compose`; SQLite and an in-process cache remain as the no-setup path |
| "clean, well-structured schemas and relationships" | Four unrelated tables | A station links to its `Place` by foreign key, so coordinates are stored once; CHECK constraints on price and coordinates |
| "pragmatic, production-ready code" | An OpenRouteService integration that had never run | Keep both providers, but test OpenRouteService against the real API before submitting |

Claude also offered to replace the key/value settings table with a single typed
row. I kept the key/value table I had asked for.

### 3.12 Latency

- **The question I raised:** is the dynamic programme costing the client
  latency, given the greedy is faster?
- **What the measurements said:** no. On a first request the optimizer is about
  30 ms of 500 to 1,600 ms; the rest is waiting for the routing server. Swapping
  algorithms would save 2 to 6%.
- **Decided by me, on Claude's recommendation:** keep the dynamic programme as
  the default and attack the real costs instead.

| Change | Measured effect | Decided by |
| --- | --- | --- |
| Ask the provider for an encoded polyline, not GeoJSON | New York to Los Angeles: 281 KB and 1.55 s down to 84 KB and 1.07 s for the raw call | Claude default |
| Cache the finished plan, not only the route | A repeat of the same trip: 70 to 100 ms down to under 20 ms | Me |
| Open the provider connection and load the station index at server start | A cold connection to OSRM cost 1,189 ms against about 185 ms warm | Me |

End to end on my machine, New York to Los Angeles went from about 1,640 ms on a
first request to about 680 ms, and from about 100 ms on a repeat to about 18 ms.
These are against the public OSRM server and vary from run to run.

One option was measured and rejected: OSRM's "simplified" route comes back in
0.6 s but has 53 points for 2,800 miles, far too coarse to match stations against.

### 3.13 Smaller calls (all Claude defaults)

- **URL and methods:** `POST` or `GET /api/v1/route/` with the same fields.
- **Errors** share one shape, `{"error": {"code", "message"}}`, with 400 for bad
  input, 422 when no route or no feasible fuel plan exists, 502 when the routing
  provider fails and 503 when it is not configured.
- **Response size:** the route line is thinned to at most 3,000 points, responses
  are gzipped (about 65 KB down to 23 KB cross-country), and
  `include_geometry=false` drops the line altogether (under 3 KB).
- **TLS:** outbound HTTPS is verified against the operating system's trust
  store. This came from a real failure: antivirus HTTPS scanning on my machine
  made every Python request fail certificate checks. Verification stays on.
- **A cache outage does not fail a request.** Cache reads and writes are wrapped;
  the trip is planned without them.

### 3.14 Making it production-ready without overbuilding

- **The question I raised:** what would make this look production-ready without
  over-engineering it?
- **Decided by me, from four options Claude offered:** API hardening, frontend
  fixes and filling the test gaps. I left out the fourth, a CI workflow.

| Added | Why |
| --- | --- |
| Rate limit per client (60 requests a minute at first, 120 since 3.15) | The API is open and every new trip costs a call to a free public server |
| `GET /healthz/` | Lets Docker (or a load balancer) know when the app can actually serve; 503 until the data is loaded |
| One JSON shape for every error, including unexpected ones | A client never gets an HTML error page, and internals are logged, not returned |
| HTTPS settings behind `DJANGO_SECURE` | Clears Django's deploy check except two HSTS options that commit a whole domain |
| Map page works on narrow screens, button shows progress | Found by taking screenshots: the map was squeezed to a strip at phone width |
| Tests for the import commands, the key command, the Nominatim client | Coverage went from 85% to 94% |
| `seed_admin`, a demo admin login for the local stack | I asked for it so the settings table can be shown in the demo; the published password is refused when debug is off |

Deliberately not added, because each would be more to explain than it shows for
a single endpoint: OpenAPI/Swagger pages, authentication, a task queue,
Kubernetes manifests, PostGIS, a JavaScript framework.

### 3.15 The page a reviewer uses

- **What I asked for:** everything a reviewer would otherwise do in Postman or
  the admin (change the cost per stop, start with less fuel, see an error, check
  the server) should be doable from the page, and the flow should feel easy.
- **How it was decided:** with clickable mockups, not descriptions.
  1. Claude built three concepts, all with a sidebar next to the map.
  2. I asked why it had to be a sidebar and proposed my own: a dialog that asks
     "Where are you right now?" and "Where are you headed?", then shows the
     route at default settings and lets you tweak from there.
  3. Claude built that and three more without a sidebar: a fuel timeline under
     the map, a conversation, and a tabbed report.
  4. I picked pieces from three of them: my onboarding, the fuel timeline, and
     the report's tables. Claude mocked three ways to lay those out and I chose
     the map on top with a tabbed panel below.
- **What the page is:** a client of the API. The server plans nothing for it;
  the script calls `POST /api/v1/route/` and the "API call" tab shows the call.
  A link with a trip in it (the API's `map_url`) opens straight on that trip.

Smaller calls, all Claude defaults that I kept:

| Call | Why |
| --- | --- |
| The two counters override one request only | They are for trying things; they change nothing for anyone else |
| The counters start from the server's current settings | So the page and the API never disagree about the defaults |
| Rate limit raised from 60 to 120 requests a minute | The "Stops against cost" tab makes four small requests when it opens and one more per step of a counter; none reaches the routing provider |
| "Planning your route" is a plain spinner | The mockup ticked off three stages on a timer, which would have been pretend progress |
| Everything from a response is escaped before it is shown | Station names and error messages echo data and user input |

Then two requests of mine about the settings:

1. **"Server settings" should open a drawer on the page**, not send you to the
   admin. Claude built it read-only, fed by a new `GET /api/v1/settings/`.
2. **The drawer should let you edit**, without an "edit in the admin" button.
   That makes the page able to write settings that apply to every client, so
   Claude put conditions on it, which I kept:

| Condition | Why |
| --- | --- |
| Saving needs a signed-in account with permission to change settings; reading stays open | Otherwise any visitor could change the routing provider for everyone |
| Sign-in happens in the drawer and is the same session as the admin | One set of accounts, nothing new to manage |
| Every write carries Django's CSRF token | A signed-in browser cannot be made to change settings from another site |
| Sign-in is limited to 10 attempts a minute | Slows down password guessing |
| All values are validated and saved together or not at all | A half-applied change could leave the planner unusable |
| The server refuses to switch to a provider that has no API key | Every request after that would fail |
| An API key can be stored from the drawer but is never sent back, and changes are logged with who made them, never the key | Same rule as the admin form |

After a save the plan on screen is redone under the new settings, so the effect
is visible at once.

3. **The API key box belongs with the provider it unlocks.** It had been in a
   separate list at the bottom of the drawer. It now appears directly under the
   routing provider choice when a provider that needs a key is selected, and
   goes away otherwise. Claude dropped the separate list, since the key status
   it showed now sits with the provider too.
4. **A link to where the key comes from**, right under the key field. It opens
   the provider's sign-up page.
5. **A gear icon instead of the "Server settings" label, and an Admin button
   beside it** that opens the admin panel. Claude made that open in a new tab,
   so the trip on screen is not lost.
6. **An icon on the Admin button too, and a third icon button for an "About"
   drawer.** Claude first filled it with my name, a short bio and links taken
   from my public GitHub profile. I trimmed the links, then removed the title,
   the initials, the name and the bio, so the drawer is only a note and links.
7. **The drawer is a note to the reviewer.** I wanted that space to speak to
   whoever is reviewing this, starting "Hey there. Yes, you." and saying the
   page was built with them in mind. Claude drafted it, I rewrote it in my own
   words, and then asked Claude to tone my version down. The text is in
   `planner/author.py`, and every claim in it is something the page really does.
8. **Zoom buttons beside "Change trip"** instead of in the map's corner. The
   map library only puts its own control in a corner, so Claude replaced it
   with a minus and plus pair inside the trip bar. They sit beside "Change trip"
   whatever the trip is called, and grey out at the zoom limits.
9. **The bottom area is a drawer, and the settings drawer sits beside the
   page.** My idea for the bottom: show only the numbers and the tab names at
   first, and raise the content when a tab is clicked. The open question was
   how that lives with the side drawer. Claude laid out three ways (side
   drawer covers everything, side drawer pushes the page aside, or only one
   open at a time) and I chose pushing, so a setting can be saved while
   watching the chart or table change. I agreed five details Claude proposed:
   the panel opens to about 45% of the window; the About drawer still covers
   and dims; a link naming a tab opens on it; Escape closes only the side
   drawer; and on a phone the side drawer covers the full width.
   After seeing it, I asked for the open drawer to be clearly smaller. It went
   from 45% of the window to 25%, then to about 16%, so the map keeps roughly
   70% of the screen. The tables scroll, which I accepted, and the chart drops
   its place-name labels at that height to keep the fuel line readable.
10. **Make the pieces of the bottom area look different from each other.** The
    numbers, the tab names and the content were all white, so they blended.
    Claude put three themes on the live page behind a temporary switcher (a
    tinted tab bar, separate cards on grey, and a dark band for the numbers)
    and recommended the dark band. I chose it: the fuel bill is the answer the
    page exists to give, so it sits on dark navy, the tab names sit on a light
    grey bar, and the open tab is white like the content it belongs to. The
    switcher was removed once I had picked.
11. **Mark the finish with a chequered flag.** I asked for a race flag at the
    end of the route and whether it would be the emoji or a drawing. Claude
    drew it as a small SVG: the emoji looks different on every operating
    system, and it has no exact point to stand on, whereas the foot of the
    drawn pole sits on the destination itself at every zoom level.
12. **Counters instead of sliders, with realistic limits.** I asked why the
    cost-per-stop slider ran from $0 to $20. Claude measured four long trips:
    every plan had stopped changing by $20 and stayed the same even at $500 a
    stop. I then asked for limits that are realistic, because a stop that
    costs nothing is not, and for counters with an arrow on each side that
    also change when you scroll over them. The limits themselves are Claude's
    proposal: cost per stop from $1 to $20 in $1 steps, and starting fuel from
    50 miles to a full tank in 50-mile steps. $1 rather than a higher floor
    because most of the change in a plan happens below $5, so a floor of $5
    would leave the counter with almost nothing to show. These are limits of
    the page only; the API still accepts any cost from $0 up, and whatever the
    server is set to is always within the counter's reach. A run of clicks or
    a spin of the wheel is sent as one request, 0.3 seconds after the last
    change.
13. **A colour for each number on the dark band.** In white they all looked
    the same to me. The fuel bill is now green, the stops blue, the miles pink
    and the gallons amber, while the two counters stay white because they are
    what you change, not what you get. Claude ran the colours through a
    colour-blindness check and dropped its first set, in which the blue and a
    violet were nearly identical to someone red-green colour-blind. Each
    number keeps its label, so colour is never the only thing telling them
    apart.
14. **"Stops against cost" follows the counter.** The tab used to compare a
    fixed list of settings ($0, $1, $2, $5, $10 and $20). I asked for it to
    show the cost I have selected with the two settings below it and the two
    above, five plans in all. At either end of the counter's range the five
    slide along, so there are always five, and clicking a row still selects
    it. Two things here are Claude's: the table scrolls to keep the selected
    row in the middle of the short drawer, since it first opened with that row
    out of sight, and rows already fetched are kept, so a step of the counter
    costs one extra request rather than four. The trade-off, which Claude
    pointed out: neighbouring $1 settings often give the same plan, so the
    five rows vary less than the old spread did.
15. **A realistic upper cap on cost per stop.** The $20 cap was where plans
    stopped changing, not a real-world figure, and I asked for a realistic
    one. Claude worked it out from two published numbers: the American
    Transportation Research Institute puts the cost of running a truck at
    $91.27 an hour (2023), and a truck's fuel stop takes 10 to 30 minutes from
    leaving the road to rejoining it. Half an hour at that rate is about $45,
    so the counter now runs from $1 to $45. Starting fuel needed no change:
    its cap is the tank. What this does not alter is the planner: on the long
    trips we measured, plans stop changing somewhere between $6 and $20, so
    the upper part of the counter is realistic but uneventful.

### 3.16 Django Debug Toolbar for the reviewer

- **What I asked for:** Django Debug Toolbar, so that a reviewer can see the
  queries and timing behind any request without reading the code first.
- **How it is set up (Claude's calls):**

| Call | Why |
| --- | --- |
| On by default in the docker compose stack, off everywhere else | The compose stack is the one a reviewer runs. Anywhere else it must be asked for with `DJANGO_DEBUG_TOOLBAR` |
| It has its own switch and does not depend on `DEBUG` | The compose stack runs with `DEBUG` off, as a deployment would, and I did not want to turn that on just for this |
| The server refuses to start with it and `DJANGO_SECURE` together | It shows SQL, headers and cache calls to every visitor, so it must not reach a real deployment by accident |
| Six panels: History, Time, SQL, Cache, Request, Headers | Every panel costs time on every request. With all of them it added about 50 ms; with these six, 25 to 30 ms |
| It follows the page's API calls | The map page is only a client of the API, so the interesting queries are behind its fetches, not behind the page itself |
| What it records is kept in Redis, in a cache of its own | There are two gunicorn workers, and either must be able to show a request the other served |
| The container's health check is left out | It runs every ten seconds and would fill the history |

- **What it cost:** 25 to 30 ms on every request while it is on. The timings in
  the README are with it off. Most of that is the toolbar's own bookkeeping
  after the answer is ready: the page's "answered in N ms" line, which is timed
  inside the view, reads only about 2 ms higher with it on.
- **What it showed straight away:** a plan is three queries taking about 2 ms
  (the settings, the start place, the finish place); a repeat of the same
  request makes three cache calls and a new plan five. It also showed a 404 for
  `/favicon.ico` on every page load, so the site now has an icon.

## 4. Things testing caught

- **GET ignored a default.** Django REST framework reads a query string like an
  HTML form, where a missing boolean means false, so GET requests silently lost
  their route geometry. An API test caught it.
- **The Docker stack failed on an empty database.** PostgreSQL reports healthy
  on its socket during first-run initialisation and then restarts; the app
  connected in between. The health check now goes over TCP.
- **A variable shadowed the Django cache** in the import command, which would
  have crashed on the new cache-clearing line. A linter caught it and there is
  now a test for the import.

- **The map showed "Access blocked" tiles.** This one I found myself, running
  the demo. OpenStreetMap refuses tile requests that carry no `Referer`, and
  Django's default `Referrer-Policy: same-origin` header stops the browser
  sending one to other sites. The tile layer now opts back in, sending the
  origin only. Earlier screenshots had happened to get real tiles, so the check
  that passed was not checking the right thing; this time the header a browser
  sends was measured directly.
- **The tab bar showed a vertical scrollbar.** I spotted this one too. Each tab
  overlapped the line under the bar by one pixel to draw its underline, and
  that pixel counted as overflow. The underline is now drawn inside the tab.
- **A quick browser check gave a wrong answer.** A screenshot method that
  fast-forwards time showed the new zoom buttons doing nothing. They worked;
  that method mishandles animation. Interactive checks now drive the browser in
  real time.
- **The tests were clearing the live cache.** Run inside the container they
  used the real Redis, and several tests empty the cache. The test run now
  always gets a private in-process cache.
- **The debug toolbar's Cache panel showed nothing.** With the toolbar keeping
  its records in the application's own cache, the panel reported zero cache
  calls for every request, including ones that plainly used the cache. The
  toolbar touches that cache before it starts watching it, and that stops the
  watching from ever being set up. Its records now go to a cache of their own,
  and the panel shows three calls for a repeated plan and five for a new one.

## 5. How it is verified

- 171 automated tests covering 95% of the Python lines, run on both SQLite and PostgreSQL 17.
- The rate limit was exercised against the running stack: with the limit then at
  60 a minute, 65 quick requests gave 60 successes and 5 refusals with a
  `Retry-After` header.
- The page was driven in a headless browser against the live API: example and
  typed trips, both counters, every tab, the error messages, a script-injection
  attempt typed into the dialog, direct links, and desktop and narrow widths.
- The Docker stack was rebuilt from an empty volume and exercised with real
  requests.
- Routes were planned against the live OSRM server throughout.

## 6. Known limitations and open items

- **OpenRouteService has not been run against the real API yet.** It needs a
  key. Until that is done, treat it as untested.
- Station positions are town-level (3.4) and detours are not costed (3.9).
- A trip shorter than the starting fuel reports $0 (3.7).
- The public OSRM server has no uptime guarantee and routes for cars, not trucks.
- The station index is loaded once per server process, so workers need a
  restart after a re-import. Cached plans live for an hour.
- There is no authentication; the brief did not ask for it. There is no CI
  workflow yet either.
- The page's JavaScript has no automated tests; it was checked by
  driving it in a headless browser (section 5).
