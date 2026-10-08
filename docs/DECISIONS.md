# Decision log

A record of how this project was built: what was found, which options were on
the table, what was chosen and why. Entries were written as the decisions were
made, with the numbers known at the time. Later entries sometimes revise
earlier ones; where they do, the earlier entry points to the later one.

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
| Latest stable Django | Django 6.1, which needs a newer Python than the development machine's default |

## 2. What is in the fuel price file

`fuel-prices-for-be-assessment.csv` was profiled before anything was designed.

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

- The brief asks for the latest stable Django, which is 6.1.2.
- The development machine's default `python` is 3.10, where pip can only
  install Django 5.2, so the project uses a virtualenv built from Python 3.14.

### 3.2 Routing: OSRM and OpenRouteService, switchable at runtime

- **Options:** OSRM's public server alone (no key, the simplest);
  OpenRouteService alone; OpenRouteService for geocoding as well as routing.
- **Chosen:** both routing providers, selectable from a settings table in the
  database, with the OpenRouteService key in a table of its own, encrypted at
  rest. OSRM is the default because it needs no key.
- **What that became:**
  - `Setting`: one row per runtime setting (`routing.provider`,
    `stations.corridor_miles`, `vehicle.range_miles`, `vehicle.mpg`,
    `stops.cost_per_stop`). Defaults live in code, so an empty table still works.
    Editable in the Django admin; a request can also name a provider.
  - `ProviderCredential`: the API key, Fernet-encrypted by a custom model field.
    The encryption key comes from the environment and several can be listed, so
    it can be rotated. The admin form is write-only.
- **The trade-off accepted:** the encrypted key still depends on one
  environment secret. What the table buys is that the provider can be switched
  and the key replaced from the admin without a deploy.
- **Status of OpenRouteService:** written to its documentation, covered by
  tests with mocked responses, and run against the live API. New York to Los
  Angeles came back at 2,812 miles (OSRM: 2,810) and Denver to Kansas City at
  598.0 (OSRM: 598.9), each in one call. Its error handling (a rejected key, no
  route, a quota) has only been exercised with mocked responses.

### 3.3 Start and finish are resolved offline

- This follows from 3.2: with no geocoding service in the design, place names
  have to be resolved locally.
- A request's "City, ST" is looked up in a table of 32,058 US places loaded from
  the Census Bureau Gazetteer (public domain). That keeps the request at exactly
  one external call. "lat,lon" is accepted too.
- The lookup tolerates "chicago il", "Chicago, Illinois", "Saint Louis" for
  "St. Louis", "New York City", and consolidated names ("Nashville" for
  "Nashville-Davidson"). An ambiguous name without a state gets a 400 that lists
  the candidates.
- Coordinates are checked against rough boxes around the lower 48 states,
  Alaska and Hawaii. That catches swapped and far-off values; it is not the
  border (see 3.19).

### 3.4 Station coordinates: Census first, Nominatim for the rest

- **Options:** Census town centres only; Nominatim (OpenStreetMap) for every
  town; Census first, with Nominatim for the towns it does not list.
- **Chosen:** the hybrid, for coverage. Census alone left several hundred
  stations without a position.
- **Result:** all 6,626 US stations are located: 6,313 by Census town centroid
  and 313, in 234 towns, by Nominatim.
- Nominatim was queried once, at one request per second, for the 242 towns the
  Census file did not match at the time. The answers are committed in
  `data/nominatim_cache.json`, so the import runs offline and gives the same
  result every time. Eight of the 242 are no longer needed, three of which had
  come back empty: all eight are "Mc" towns, which the Census file matches
  since the "Mc Lean" fix below. As a sanity check, every Nominatim position is
  within 25 miles of a Census place in the same state.
- **Problems found in the data along the way** (each one was costing stations):
  accented names ("Cañon City") were being mangled by the name normalisation; a few
  Census entries are filed as "Town of Pecos"; the fuel file writes "Mc Lean"
  where the Census writes "McLean".
- **Accepted limitation:** a station sits at the centre of its town, not at its
  exit. That is typically a few miles off, which is small against a 500 mile
  range but means `miles_off_route` in the response is approximate. The 6,626
  stations share 3,808 positions.

### 3.5 Canadian stations are dropped

- The brief limits trips to the USA, so the 620 Canadian rows are excluded at
  import. A few US-to-US routes are shortest through Ontario; those stations
  will not be offered.

### 3.6 Duplicate IDs take the mean price

- **Options:** the mean, the lowest or the highest of the listed prices.
- **Chosen:** the mean. 597 truck stops are listed more than once with
  different prices and nothing says which row is right. The mean does not
  flatter the total; the lowest would understate what a driver pays.

### 3.7 The vehicle starts with a full tank, overridable per request

- **Options:** full and overridable; always full; start empty.
- **Chosen:** full, with `initial_range_miles` in the request to start with
  less.
- "Total money spent" therefore means fuel bought on the way, and a trip under
  500 miles has no stops and a `total_fuel_cost` of $0. That is correct under
  this model and reads badly, so the response later gained a second figure for
  all the fuel burned (3.19).
- "Start empty" was rejected because it needs a fudge: the vehicle has to be
  given just enough fuel to reach its first station.

### 3.8 Choosing the stops: cheapest fuel, then a cost per stop

This took two rounds.

- **Round 1:** the optimal look-ahead greedy, chosen over "cheapest station in
  each window" (which is not optimal) and a general dynamic programme (heavier
  for the same answer). At each station: if a cheaper one is within range, buy
  just enough to reach it; otherwise fill up and go to the cheapest in range.
  This provably minimises the fuel bill.
- **What running it showed:** the cheapest plan is not a plan anyone would
  drive. Seattle to Miami came out as 20 stops, one of them for 0.13 gallons
  ($0.38), because chasing every slightly cheaper pump costs nothing in the model.
- **Round 2:** three options. Leave it; filter out small purchases afterwards,
  which would no longer be optimal for any stated objective; or minimise the
  fuel bill plus a fixed cost per stop. The third was chosen, with a default of
  $5 that is a setting, and 0 restoring the pure cheapest plan.

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
- **How they are known to be right:** the greedy matches an independent formula
  on 300 random routes; the dynamic programme matches brute force over every
  subset of stations on 400 small random routes; with a negligible stop cost
  the two agree on 200 larger ones.

### 3.9 Which stations count as "on the route"

- A station counts if it is within 5 miles of the route line (a setting). The
  detour to reach it is not added to the trip. Five miles is deliberately
  forgiving because station positions are town centres (3.4).
- Matching is done in memory with numpy, not in SQL. PostGIS would be the
  database-native way to do this, and the right move with much more data, but
  it needs GDAL installed to run the project at all, and 6,626 stations fit
  comfortably in memory. Measured on a cross-country route: comparing every
  station to every route point took 117 ms; a coarse grid that first narrows
  6,626 stations to the few hundred near the route brought it to about 12 ms.

### 3.10 What "return a map" means

- The API returns JSON with the route as GeoJSON, which any map client can draw.
  `/map/` renders the same plan on an interactive Leaflet map, for a person.

### 3.11 What changed after reading the job description

The job description arrived after the first working version. Three things in it
were not reflected in the project, all of them things that had never been
explicitly decided:

| The job description says | What there was | What was decided |
| --- | --- | --- |
| "we use PostgreSQL" | SQLite, by default | PostgreSQL and Redis through `docker compose`; SQLite and an in-process cache remain as the no-setup path |
| "clean, well-structured schemas and relationships" | Four unrelated tables | A station links to its `Place` by foreign key, so coordinates are stored once; CHECK constraints on price and coordinates |
| "pragmatic, production-ready code" | An OpenRouteService integration that had never run | Keep both providers, and run OpenRouteService against the real API before submitting (done: 3.2) |

Replacing the key/value settings table with a single typed row was also
considered, and not taken. The key/value table means a new setting needs no
schema migration, at the price of values stored as text and parsed on read.

### 3.12 Latency

- **The question:** is the dynamic programme costing the client latency, given
  that the greedy is faster?
- **What the measurements said:** no. On a first request the optimizer is about
  30 ms of 500 to 1,600 ms; the rest is waiting for the routing server. Swapping
  algorithms would save 2 to 6%.
- **Decision:** keep the dynamic programme as the default and go after the real
  costs instead.

| Change | Measured effect |
| --- | --- |
| Ask the provider for an encoded polyline, not GeoJSON | New York to Los Angeles: 281 KB and 1.55 s down to 84 KB and 1.07 s for the raw call |
| Cache the finished plan, not only the route | A repeat of the same trip: 70 to 100 ms down to under 20 ms |
| Open the provider connection and load the station index at server start | A cold connection to OSRM cost 1,189 ms against about 185 ms warm |

End to end on the development laptop, New York to Los Angeles went from about
1,640 ms on a first request to about 680 ms, and from about 100 ms on a repeat
to about 18 ms. These are against the public OSRM server and vary from run to
run.

One option was measured and rejected: OSRM's "simplified" route comes back in
0.6 s but has 53 points for 2,800 miles, far too coarse to match stations against.

### 3.13 Smaller calls

- **URL and methods:** `POST` or `GET /api/v1/route/` with the same fields.
- **Errors** share one shape, `{"error": {"code", "message"}}`, with 400 for bad
  input, 422 when no route or no feasible fuel plan exists, 502 when the routing
  provider fails and 503 when it is not configured.
- **Response size:** the route line is thinned to at most 3,000 points, responses
  are gzipped (about 65 KB down to 23 KB cross-country), and
  `include_geometry=false` drops the line altogether (under 3 KB).
- **TLS:** outbound HTTPS is verified against the operating system's trust
  store. This came from a real failure: antivirus HTTPS scanning on the
  development machine made every Python request fail certificate checks.
  Verification stays on.
- **A cache outage does not fail a request.** Cache reads and writes are wrapped;
  the trip is planned without them.

### 3.14 Making it production-ready without overbuilding

- **The question:** what would make this production-ready without
  over-engineering it?
- **Options:** API hardening, frontend fixes, filling the test gaps, a CI
  workflow. The first three were taken. CI was left out at this point and came
  in later with the linter (3.17).

| Added | Why |
| --- | --- |
| Rate limit per client (60 requests a minute at first, 120 since 3.15) | The API is open and every new trip costs a call to a free public server |
| `GET /healthz/` | Lets Docker (or a load balancer) know when the app can actually serve; 503 until the data is loaded |
| One JSON shape for every error from an API endpoint, including unexpected ones | A client of the API never has to parse an HTML error page, and internals are logged, not returned |
| HTTPS settings behind `DJANGO_SECURE` | Clears Django's deploy check except two HSTS options that commit a whole domain |
| Map page works on narrow screens, button shows progress | Found by taking screenshots: the map was squeezed to a strip at phone width |
| Tests for the import commands, the key command, the Nominatim client | Coverage went from 85% to 94% |
| `seed_admin`, a demo admin login for the local stack | So that the settings table can be shown in a demo. The command will not fall back to the published password when debug is off; the compose stack passes it in explicitly and is reachable from the local machine only (3.19) |

Deliberately not added, because each would be more to explain than it shows for
a single endpoint: OpenAPI/Swagger pages, credentials on the route endpoint, a
task queue, Kubernetes manifests, PostGIS, a JavaScript framework. (Sign-in was
added later, for changing the server settings only: 3.15.)

### 3.15 The page a reviewer uses

- **The aim:** everything a reviewer would otherwise do in Postman or the admin
  (change the cost per stop, start with less fuel, see an error, check the
  server) can be done from the page, and the flow is easy.
- **How the layout was decided:** with clickable mockups, not descriptions.
  Three concepts with a sidebar beside the map came first. They were set aside
  for a dialog that asks "Where are you right now?" and "Where are you
  headed?", then shows the route at default settings and lets the visitor
  adjust from there. Three more concepts without a sidebar followed (a fuel
  timeline under the map, a conversation, a tabbed report), and the page is
  built from pieces of them: the dialog, the timeline and the report's tables,
  with the map on top and a tabbed panel below.
- **What the page is:** a client of the API. The server plans nothing for it;
  the script calls `POST /api/v1/route/` and the "API call" tab shows the call.
  A link with a trip in it (the API's `map_url`) opens straight on that trip.

| Call | Why |
| --- | --- |
| The two counters override one request only | They are for trying things; they change nothing for anyone else |
| The counters start from the server's current settings | So the page and the API never disagree about the defaults |
| Rate limit raised from 60 to 120 requests a minute | The "Stops against cost" tab makes four small requests when it opens and one more per step of a counter; none reaches the routing provider |
| "Planning your route" is a plain spinner | The mockup ticked off three stages on a timer, which would have been pretend progress |
| Everything from a response is escaped before it is shown | Station names and error messages echo data and user input |

**Settings on the page.** "Server settings" opens a drawer on the page instead
of sending the visitor to the admin. It began read-only, fed by a new
`GET /api/v1/settings/`, and then became editable. That makes the page able to
write settings that apply to every client, so editing came with conditions:

| Condition | Why |
| --- | --- |
| Saving needs a signed-in account with permission to change settings; reading stays open | Otherwise any visitor could change the routing provider for everyone |
| Sign-in happens in the drawer and is the same session as the admin | One set of accounts, nothing new to manage |
| Every write carries Django's CSRF token | A signed-in browser cannot be made to change settings from another site |
| Sign-in is limited to 10 attempts a minute | Slows down password guessing |
| All values are validated and saved together or not at all | A half-applied change could leave the planner unusable |
| The server refuses to switch to a provider that has no API key | Every request after that would fail |
| An API key can be stored from the drawer but is never sent back by the API, and changes are logged with who made them, never the key | The admin's form is write-only in the same way; its list shows a key's last four characters so that keys can be told apart |

After a save the plan on screen is redone under the new settings, so the effect
is visible at once.

**Details settled along the way:**

| What | Why, and what else was considered |
| --- | --- |
| The API key box sits directly under the routing provider choice, shows only for a provider that needs a key, and links to where a key comes from | It had been in a separate list at the bottom of the drawer, away from the thing it unlocks |
| Three icon buttons: a gear for the settings, a shield that opens the admin, a person for an "About" drawer | The admin opens in a new tab, so the trip on screen is not lost |
| The About drawer is a short note to the reviewer and a few links, kept in `planner/author.py` | It first held a name, a bio and a longer list of links, which were cut back. Every claim in the note is something the page does |
| Zoom buttons beside "Change trip" | The map library only puts its own control in a corner of the map. The pair greys out at the zoom limits |
| The bottom area is a drawer: only the numbers and the tab names show until a tab is clicked | It gives the map the room. The open drawer went from 45% of the window to 25% and then to about 16%, so the map keeps roughly 70% of the screen. The tables scroll, and the chart drops its place-name labels at that height |
| The settings drawer pushes the page aside | Chosen over a drawer that covers everything and over allowing one drawer at a time, so a setting can be saved while watching the chart or table change. The About drawer still covers and dims, Escape closes only the side drawer, and on a phone the side drawer takes the full width |
| A dark band for the numbers, a light grey bar for the tab names, white for the open tab | All three had been white and blended together. Three themes were tried on the live page (a tinted tab bar, separate cards on grey, the dark band). The fuel bill is the answer the page exists to give, so it gets the dark band |
| A drawn chequered flag at the finish | The emoji looks different on every operating system and has no exact point to stand on. The foot of the drawn pole sits on the destination at every zoom level |
| Counters with an arrow on each side, which also change when scrolled over, in place of sliders | A run of clicks or a spin of the wheel is sent as one request, 0.3 seconds after the last change |
| Cost per stop runs from $1 to $45; starting fuel from 50 miles to a full tank | See below |
| A colour for each number on the band: fuel bill green, stops blue, miles pink, gallons amber. The two counters stay white | In white they all read the same. A first set of colours was dropped after a colour-blindness check: its blue and violet were nearly identical to someone red-green colour-blind. Each number keeps its label, so colour is never the only thing telling them apart |
| "Stops against cost" shows the selected cost with the two settings below it and the two above | It used to compare a fixed list ($0, $1, $2, $5, $10, $20). The five slide along at either end of the range, the table scrolls to keep the selected row in view, and rows already fetched are kept, so a step of the counter costs one request, not four. The trade-off: neighbouring $1 settings often give the same plan, so the rows vary less than the old spread did |

**The counters' limits.** The first slider ran from $0 to $20. On four long
trips every plan had stopped changing by $20 and stayed the same even at $500 a
stop, so $20 was where the planner goes quiet, not a real-world figure. A stop
that costs nothing is not realistic either. The floor is $1 and not higher
because most of the change in a plan happens below $5. The ceiling is half an
hour of a truck's running cost: the American Transportation Research Institute
puts that cost at $91.27 an hour (2023) and a fuel stop takes 10 to 30 minutes,
which gives about $45. These are limits of the page only; the API accepts any
cost from $0 up. On the trips measured, plans stop changing somewhere between
$6 and $20, so the upper part of the counter is realistic but uneventful.

**Showing what the plan was chosen from.** The planner does not compute
several routes that could be drawn faintly: it asks for one route and chooses
among the stations along it. Three things could be drawn instead, and two were.

- *The stations passed over.* Orange dots along the route. This needed the API
  to return them, so a request can ask for `candidate_stations`; it is off by
  default to keep the normal response small. Stations sit at their town's
  centre, so the first version stacked several dots on one spot. It is now one
  dot per town, listing that town's stations and prices, and the dots shrink
  when the map is zoomed out, where they would otherwise smother the route.
- *The stops of the neighbouring plans.* While "Stops against cost" is open, a
  ring marks each stop one of the other four plans would make and this one does
  not. Pointing at a row of the table fades every stop that is not in that
  row's plan; without that, the rings did not say which plan they belonged to.
- *Real alternative routes from the routing provider* were left out: they
  change the provider, the optimiser, the cache and the response, for one or
  two alternatives on a long trip.

**A GeoJSON tab.** The plan as one FeatureCollection (the route line, its two
ends and each fuel stop) with a Copy button, for pasting into geojson.io. A
tick box adds the stations passed over; it is off by default, since they turn
10 features into about 350. Colour and symbol properties make geojson.io draw
the route blue and number the stops. It is built in the page from the API's
answer, so the API did not change.

**Stops are drawn on the route.** On the first map the stops did not touch the
route line. The cause is the data: the price file gives a station's town and an
address like "I-80 Exit 223" but no coordinates, so each station sits at its
town's centre, up to five miles from the road (1.6 miles is typical). Three
remedies: draw each station at the nearest point of the route; geocode 6,626
exit addresses; or route through the stops with a second routing call. The
first was chosen. It is the honest one for stations that are at exits on the
route anyway, it costs no request, and it changes nothing in the plan.
Everything was moved onto the line at first, the orange dots included. That
lost the scatter of dots beside the route, which shows the band of country the
planner chooses from, so the dots went back to their towns. The stops, and the
rings for the other plans' stops, are drawn on the line; the stations passed
over stay at their town centres; a station shifts when it becomes a stop. The
GeoJSON tab uses the same positions. The API still reports the town centre and
`miles_off_route`, and a stop's pop-up says how far away its town is. The brief
asks for "a map of the route along with optimal location to fuel up along the
route"; it does not say how a station must be placed, and its own price file is
what limits the precision.

### 3.16 Django Debug Toolbar for the reviewer

- **The aim:** a reviewer can see the queries and timing behind any request
  without reading the code first.

| Call | Why |
| --- | --- |
| On by default in the docker compose stack, off everywhere else | The compose stack is the one a reviewer runs. Anywhere else it must be asked for with `DJANGO_DEBUG_TOOLBAR` |
| It has its own switch and does not depend on `DEBUG` | The compose stack runs with `DEBUG` off, as a deployment would, and that was not worth turning on just for this |
| The server refuses to start with it and `DJANGO_SECURE` together | It shows SQL, headers and cache calls to every visitor, so it must not reach a real deployment by accident |
| Six panels: History, Time, SQL, Cache, Request, Headers | Every panel costs time on every request. With all of them it added about 50 ms; with these six, 25 to 30 ms |
| It follows the page's API calls | The map page is only a client of the API, so the interesting queries are behind its fetches, not behind the page itself |
| What it records is kept in Redis, in a cache of its own | There are two gunicorn workers, and either must be able to show a request the other served |
| The container's health check is left out | It runs every ten seconds and would fill the history |

- **What it costs:** 25 to 30 ms on every request while it is on. The timings in
  the README are with it off. Most of that is the toolbar's own bookkeeping
  after the answer is ready: the page's "answered in N ms" line, which is timed
  inside the view, reads only about 2 ms higher with it on.
- **What it showed straight away:** a plan is three queries taking about 2 ms
  (the settings, the start place, the finish place); a repeat of the same
  request makes three cache calls and a new plan of a known trip four (five
  before the cache was reworked in 3.18). It also showed a 404 for
  `/favicon.ico` on every page load, so the site now has an icon.

### 3.17 A review of the code's structure, and six fixes

The code was re-read and checked for modularity, readability, maintainability
and Django practice. The verdict was "mostly, on the Python side", with six
shortfalls. All six were fixed, with reuse in mind.

| Shortfall | What was done |
| --- | --- |
| The settings endpoint was 58 lines of permission checks, validation and writes inside the view | A permission class says who may write, a serializer says what a change may contain, and `services/server_settings.py` reads and writes the tables. The view is a few lines |
| Signing in reached into a private attribute of the request and called the CSRF check by hand | An authentication class checks the token on every writing request, signed in or not |
| The planner built the API's JSON itself, rounding and key names included | `plan_trip()` returns typed objects (`Trip`, `TripPlan`, `FuelStop`, `Station`) and serializers turn them into JSON |
| Nothing enforced style | A ruff configuration, the code formatted with it, and a CI workflow that runs it with the tests |
| Eight tests matched the text of the CSS and JavaScript | Tests that drive the page in a real browser instead |
| A module-level HTTP session, a global station index behind a lock, an import inside a function, leftover `startproject` comments | Each provider owns its session, the index is a cached loader, `conf.py` holds definitions only, and the comments describe this project |

- **Checked, because a refactor proves nothing by itself:** eight API requests
  compared field by field between the code before and after came out
  identical, key order included. Response times are what they were (a repeated
  plan with the station list: 27 ms before and after, on a local run). Each of
  three deliberate breakages of the page was caught by the new browser tests.
- **One visible change:** a visitor who sends POST, PUT or DELETE to the
  settings is told to sign in (401) instead of that the method does not exist
  (405). That is the order REST framework checks in, and it was kept.
- **A cost that had to be undone:** declared field by field, the serializer for
  the station list added about 8 ms to every request that asks for it, since
  there can be several hundred stations. It now writes each one out directly.
- **CI after all:** a CI workflow was left out in 3.14. Enforcing the linter
  needs something to run it, so there is one now. On the first push that
  included it, all three jobs passed: the linter, the tests on SQLite with the
  browser tests, and the tests on PostgreSQL.
- **What this bought in reuse:** the planner can be called from a command, a
  report or another API version and returns the same objects; the
  `set_provider_key` command and the API store a key through one function
  where they had two; and the test trip is one fixture shared by the API tests
  and the browser tests.

### 3.18 A review of the caching, the queries and the algorithm

A real New York to Los Angeles plan was measured part by part, to see whether
the technical side holds up: caching, query times, time and space complexity.

- **What held:** three indexed queries per plan (about 2 ms); station matching
  in 15 ms; the dynamic programme in 37 ms, with time growing as n × w × log w
  and memory as n × w (n stations on the route, w within one tank of each
  other). Doubling the trip length doubled its time and doubling the density
  quadrupled it, on synthetic data.
- **What did not**, with the option chosen for each:

| Weakness | Decision | Why |
| --- | --- | --- |
| Redis had no memory limit. A client at the rate limit could add about 80 MB a minute | Cap it at 256 MB and drop the least recently used entries | A cache should be allowed to forget. 256 MB holds about 2,300 cross-country trips |
| Every cached plan repeated the route's line and station list: 109 KB each, on top of 549 KB for the route as fetched | Keep only the route made ready for planning (108 KB) and the stops per plan (1.4 KB). Drop the route as fetched | A trip with one plan goes from 658 KB to 109 KB, and each further plan from 109 KB to 1.4 KB. The price: changing the corridor setting costs each trip one new routing call |
| Identical new requests arriving together each called the routing server | The first calls it; the others wait for its answer | Ten people asking for the same new trip now cost one call, across both workers |
| The corridor setting had no upper limit, and the optimiser's time grows with the square of the stations in it | No wider than 25 miles | 0.16 s to choose the stops at 25 miles against over a second at 100, and past 25 a station is hard to call "on the route" |

- **A side effect worth having:** a new plan of a known trip no longer matches
  the stations again, so it dropped from about 60 ms to about 42 ms inside the
  planner.
- **Still true:** a routing call can wait up to 20 seconds and there are eight
  request threads, so a hanging provider can tie the server up. That has not
  been changed.

### 3.19 A review as a sceptical reader would make it

The repository was read cold, the way a reviewer with no context would read
it, and each thing found was then run before it was believed. What was
confirmed, and what was done about it:

| Found | Done |
| --- | --- |
| The rate limits believed `X-Forwarded-For` as sent. With a new value on each request, 8 of 8 requests passed a limit of 2 a minute | The limits count the address the connection comes from. The header is believed only for as many proxies as `DJANGO_NUM_PROXIES` says stand in front, which is none by default. The sign-in limit also applies to a session that is already signed in |
| The compose stack, with its published admin password and the debug toolbar, was reachable from the network | Its port is published to the local machine only |
| When the request fetching a new route failed or timed out, every request waiting on it called the provider at the same moment | One of them takes over and the rest keep waiting, for no longer than a routing call may take |
| With `DEBUG` off, an error outside the API (the page, the admin, the health check) reached no log | Every response of 500 and above is logged |
| A stored API key that could not be decrypted crashed the request | It is answered with 503 `routing_provider_not_configured`, as documented |
| A stored setting that is no longer allowed, such as a corridor saved before the 25-mile ceiling, would have failed every request | It is passed over for the default, with a warning in the log |
| "Azusa" lost its last three letters to the optional "USA" suffix, and "West New York" was read as "West" in New York | A country needs a comma or a space before it, and a whole name is tried before a trailing state is split off |
| One box from Hawaii to Alaska counted as "inside the USA", which took in Mexico City and half the Pacific | Three boxes: the lower 48, Alaska and Hawaii |
| The thinned route line could have 3,001 points | Never more than 3,000 |
| `POST /api/v1/route` without its last slash was redirected, which loses the body | Both forms are served |
| On the page, a link that named a provider was planned with the default one, and the curl command shown broke on an apostrophe | Both fixed |

**The fuel cost of a short trip.** A trip the starting tank covers reported $0
(3.7), which is the first thing someone trying Dallas to Austin sees. Three
options: add a second figure for all the fuel burned; only spell out the
starting fuel in the response; or leave it, since it is documented. The first
was chosen. `total_fuel_cost` keeps its meaning, and `summary.fuel_used_cost`
prices every gallon burned, the starting fuel included, at the plan's average
price per gallon, or at the cheapest station on the route when nothing is
bought. Dallas to Austin shows $0.00 on the way and $54.85 in all; Chicago to
Houston $170.02 and $315.81. The page shows both numbers side by side.

**The documents.** The README now leads with the brief and where to check each
point of it, puts everything beyond the brief under one heading, and went from
420 lines to 291. Statements in it and in this log that were no longer
true were corrected, among them that OpenRouteService had not run live (3.2),
that the CI workflow had not run (3.17), and what `seed_admin` refuses (3.14).

**Looked at and left as it is:**

- *A real border check for coordinates.* It needs a boundary file, and at any
  resolution worth shipping it still misjudges points within a few miles of
  the border or the coast, where rejecting a real US address is worse than
  accepting Windsor, Ontario. The brief says both ends are in the USA. The
  boxes stay, described as what they are: a point just across the border, such
  as Toronto, is accepted and routed like any other.
- *An ambiguous name* still gets a 400 listing the candidates, not a guess at
  the largest. "New York" alone is ambiguous because Florida has one too.
- *Station positions* are still town centres (3.4), and 6,626 stations share
  3,808 of them.
- *The scope.* The brief asks for one endpoint. The page, the settings
  endpoint with its sign-in, the second routing provider and the debug toolbar
  go beyond it. They stay, and the README says which is which.
- *A hanging routing provider* can still occupy request threads (3.18).

### 3.20 The two location boxes search as a name is typed

The dialog's two boxes were plain text: a visitor had to know to write
"Chicago, IL", and a name shared by several places came back as an error to
correct. They now offer places while a name is being typed.

| Question | Options | Chosen, and why |
| --- | --- | --- |
| Where do the names come from | Send all 32,058 places to the browser; search them in memory on the server; search the table | The table, through a new `GET /api/v1/places/?q=`. The whole list is about 500 KB for a page that needs eight names, and the table is already there. The page keeps what it has already been told, and the answer may be kept by the browser for an hour |
| How the table is searched | A scan of the table; an index that serves `LIKE 'chi%'` | The index. PostgreSQL only uses one for a prefix when it is built with a pattern operator class, which the existing index on the name and state is not, so a second index was added. The query plan shows it in use: 79 rows read for "chi", 0.4 ms. The whole search takes about 2 ms |
| What comes first | Largest land area; most fuel stations in the town; largest, with Alaska and Hawaii after the rest | The third. The Census file has no population, so land area stands in for size. Tried on fifty well-known cities by their first three to five letters, land area alone put the city first for 45 and stations-first for 39: Alaska's census places are thousands of square miles of wilderness, and "chi" offered Chistochina before Chicago. With those two states listed last it was 48, and 50 of 50 once a consolidated city's short form ("Nashville") is offered ahead of its full name. The planner cannot plan a drive to either state in any case |
| What the list is on the page | The browser's own `datalist`; a list drawn by the page | Drawn by the page. With a `datalist` the browser decides what to show by comparing the letters typed with each name, so "st l" would not be shown "St. Louis", nor "ft w" "Fort Worth", although the server matches both |
| Whether it picks for the visitor | Fill in the first match; offer and wait | Offer and wait. Enter sends what is typed unless an arrow key or a click has chosen a row, so a "lat,lon" or a town the list does not offer still goes through, and the box works as before if the search fails |
| A rate limit | The route endpoint's; one of its own; none | None. It is one indexed query with no outside call, like reading the settings, and a limit would add two cache calls to a request that takes 2 ms |

A state narrows the list, with a comma or without ("springfield, m",
"springfield mo"). A consolidated city and its short form, such as
Nashville-Davidson and Nashville, are offered once, under the short form.
What the list shows is written as text, never as markup, like everything else
that comes from data.

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
- **The map showed "Access blocked" tiles.** Found while running the demo.
  OpenStreetMap refuses tile requests that carry no `Referer`, and Django's
  default `Referrer-Policy: same-origin` header stops the browser sending one
  to other sites. The tile layer now opts back in, sending the origin only.
  Earlier screenshots had happened to get real tiles, so the check that passed
  was not checking the right thing; this time the header a browser sends was
  measured directly.
- **The tab bar showed a vertical scrollbar.** Also found by eye. Each tab
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
  and the panel showed three calls for a repeated plan and five for a new one.
- **A new test passed on the code it was written to fail.** The first test for
  "waiting requests take turns" (3.19) ran five threads against a provider that
  failed once. It passed before the fix as well, because the threads rarely
  looked at the same instant. It was replaced by one that sets up the exact
  moment and fails on the old code every time.

- **`localhost` cost a fifth of a second per request.** Timing the place
  search on the Docker stack gave 240 ms where the search itself takes 2. The
  stack had just been published to `127.0.0.1` only (3.19), and curl on Windows
  tries the IPv6 loopback first when given the name `localhost`: 205 ms to
  connect, against 1 ms by address. Browsers were not affected. The README, the
  Postman collection and the examples now use `127.0.0.1`, as Django's own
  development server does.

## 5. How it is verified

- 234 automated tests covering 95% of the Python lines, run on both SQLite and
  PostgreSQL 17. Twenty-six of them drive the page in a real browser; those
  run where a browser is installed and are skipped in the Docker image.
- A GitHub Actions workflow runs the linter and both test runs on every push.
- The rate limit was exercised against the running stack: with the limit then at
  60 a minute, 65 quick requests gave 60 successes and 5 refusals with a
  `Retry-After` header.
- Before the browser tests existed, the page was driven by hand-written
  scripts in a headless browser against the live API after each change:
  example and typed trips, both counters, every tab, the error messages, a
  script-injection attempt typed into the dialog, direct links, and desktop and
  narrow widths.
- The Docker stack was rebuilt from an empty volume and exercised with real
  requests: it migrated, loaded 32,058 places and 6,626 stations, created the
  admin login and was healthy in about 16 seconds.
- Routes were planned against the live OSRM server throughout, and against the
  live OpenRouteService API as described in 3.2.

## 6. Known limitations and open items

- Station positions are town-level (3.4) and detours are not costed (3.9).
- `total_fuel_cost` is the fuel bought on the way, so a trip shorter than the
  starting fuel reports $0 there; `fuel_used_cost` is the figure that counts
  the starting fuel (3.19).
- Coordinates are checked against rough boxes, not the border (3.19).
- The public OSRM server has no uptime guarantee and routes for cars, not trucks.
- OpenRouteService's error handling has only been exercised with mocked
  responses (3.2).
- The station index is loaded once per server process, so workers need a
  restart after a re-import. Cached plans live for an hour.
- The route endpoint takes no credentials; the brief did not ask for any.
- A hanging routing provider can tie up the eight request threads (3.18).
- The page's script is tested through the browser only. It is one file of
  about 1,100 lines, and its functions have no unit tests of their own.
