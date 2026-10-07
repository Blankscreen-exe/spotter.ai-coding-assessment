# Fuel route planner

A Django API that takes a start and finish in the USA and returns the driving
route, the cheapest places to refuel along it, and the total fuel cost, for a
vehicle with a 500 mile range that does 10 miles per gallon.

- One call to a free routing API per request (OSRM by default), none on a repeat.
- Fuel stops are chosen by an exact optimizer, not a heuristic.
- All 6,626 US stations in the supplied price file are used.

How and why it was built this way is in [docs/DECISIONS.md](docs/DECISIONS.md).

## Run it

### With Docker (PostgreSQL + Redis)

```bash
docker compose up --build
```

The first start runs the migrations and loads the reference data, which takes a
few seconds. Then open http://localhost:8000/.

This stack starts with [Django Debug Toolbar](https://django-debug-toolbar.readthedocs.io/)
switched on: the green tab on the right edge of every page. Click it to see the
SQL queries, cache calls and timing behind a request. On the map page it
follows the API calls the page makes, so after planning a trip the panels
describe that call, and its History panel lists every request with a Switch
button. The toolbar adds roughly 25 to 30 ms to each request. To run without it:

```bash
DJANGO_DEBUG_TOOLBAR=false docker compose up     # or put that line in .env
```

### Without Docker (SQLite)

Developed and tested on Python 3.14. Django 6.1 needs 3.12 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py import_places    # US towns from the Census Gazetteer
python manage.py import_stations  # fuel stations from the price file
python manage.py runserver
```

No environment variables are needed for this path. `.env.example` lists the
optional ones; `DJANGO_DEBUG_TOOLBAR=true` gives this run the same toolbar.

## The page

`/map/` is the quickest way to try everything. It is a client of the API below
and nothing more: every plan on screen came from `POST /api/v1/route/`.

1. It asks "Where are you right now?" and "Where are you headed?", or offers
   four example trips, one of which is an error on purpose.
2. It then shows the route and stops on a map, the fuel bill, and two counters
   (cost per stop, starting fuel). Click an arrow beside one, or scroll over
   it, and the trip re-plans.
3. Along the bottom are five tab names. Clicking one raises a panel: the fuel
   in the tank along the trip, the fuel plan as a table, the same trip at the
   two cost-per-stop settings either side of yours, the API call behind the
   plan, or server details. Clicking it again lowers the panel and gives the map the room back.

Pointing at a stop on the map, the chart or the table highlights it in the
others. The map also shows what the plan was chosen from. The grey dots along
the route are the towns whose stations were considered and passed over; zoom in
and point at one for the stations there and their prices. While the third tab
is open, rings mark where the neighbouring plans would stop instead, and
pointing at a row of its table picks out that row's plan on the map.

The gear button (top right) opens a drawer with the server's current
settings. It sits beside the page instead of covering it, so a saved change
can be watched taking effect.
Signed in with an admin account you can change them there, and store an
OpenRouteService key; the plan on screen is then redone under the new
settings. Beside it, the shield button opens the Django admin and the person
button opens a drawer with a note from the author and links, kept in
`planner/author.py`. A link such as `/map/?start=Chicago, IL&finish=Houston, TX` opens
straight on that trip, which is what `map_url` in an API response is.

## The API

`POST /api/v1/route/` with a JSON body, or `GET` with the same fields in the
query string.

| Field | Required | Meaning |
| --- | --- | --- |
| `start`, `finish` | yes | `"City, ST"` (also `"City, State name"`) or `"lat,lon"` |
| `initial_range_miles` | no | Fuel on board at the start, in miles. Default: a full tank (500) |
| `stop_cost` | no | Dollars one extra stop is worth avoiding. Default 5; 0 gives the lowest possible fuel bill |
| `provider` | no | `osrm` or `openrouteservice`. Default: the `routing.provider` setting |
| `include_geometry` | no | `false` leaves the route line out of the response. Default `true` |
| `include_candidates` | no | `true` adds `candidate_stations`: every station the planner chose from. Default `false` |

```bash
curl -X POST http://localhost:8000/api/v1/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Chicago, IL", "finish": "Houston, TX"}'
```

```json
{
  "start": {"query": "Chicago, IL", "name": "Chicago, IL", "lat": 41.837045, "lon": -87.684939},
  "finish": {"query": "Houston, TX", "name": "Houston, TX", "lat": 29.785743, "lon": -95.388806},
  "summary": {
    "distance_miles": 1083.1,
    "duration_hours": 19.89,
    "fuel_stops": 2,
    "total_fuel_cost": 170.02,
    "gallons_purchased": 58.31,
    "gallons_used": 108.31,
    "currency": "USD"
  },
  "vehicle": {"max_range_miles": 500.0, "miles_per_gallon": 10.0, "initial_range_miles": 500.0},
  "planning": {"stop_cost": 5.0, "corridor_miles": 5.0},
  "fuel_stops": [
    {
      "order": 1,
      "station_id": 66643,
      "name": "DEERFIELD TRAVEL CENTER",
      "address": "I-55, EXIT 8",
      "city": "Steele",
      "state": "MO",
      "lat": 36.09479,
      "lon": -89.862102,
      "mile_marker": 448.6,
      "miles_off_route": 2.9,
      "price_per_gallon": 2.976,
      "gallons_on_arrival": 5.14,
      "gallons_purchased": 28.84,
      "cost": 85.8
    },
    {"order": 2, "name": "Quiktrip #7900", "city": "Texarkana", "state": "TX", "mile_marker": 788.4, "cost": 84.22}
  ],
  "route": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-87.68494, 41.83705]]}},
  "meta": {
    "routing_provider": "osrm",
    "routing_api_calls": 1,
    "served_from": "routing provider",
    "stations_considered": 158,
    "elapsed_ms": 334.2
  },
  "map_url": "http://localhost:8000/map/?start=Chicago%2C+IL&finish=Houston%2C+TX"
}
```

(The second stop and the route line are shortened here.)

- `total_fuel_cost` is the fuel bought on the way. The vehicle starts full by
  default and arrives empty, so `gallons_purchased` is less than `gallons_used`.
- `route` is GeoJSON and can be drawn by any map client. `map_url` opens the
  same plan on an interactive map.
- `candidate_stations`, when asked for, lists every station within the corridor
  in route order, the chosen ones included, with the same fields as a fuel stop
  up to its price. `meta.stations_considered` is how many there are. It is left
  out by default because it is large: 70 to 85 KB for a cross-country trip.
- `meta.served_from` is `routing provider`, `route cache` or `plan cache`, and
  `meta.routing_api_calls` is 1 or 0 accordingly.

Errors all have the shape `{"error": {"code": "...", "message": "..."}}`:

| Status | `code` | When |
| --- | --- | --- |
| 400 | `invalid_request` | A field is missing or malformed (`fields` lists them) |
| 400 | `location_not_found` | A place cannot be resolved, or its name is ambiguous without a state |
| 422 | `route_not_found` | No driving route exists between the two points |
| 422 | `no_feasible_fuel_plan` | A stretch of the route has no station within range |
| 429 | `throttled` | More than 120 requests a minute from one client (`Retry-After` says when to retry) |
| 500 | `internal_error` | Anything unexpected; details go to the server log, not the response |
| 502 | `routing_provider_error` | The routing API failed or timed out |
| 503 | `routing_provider_not_configured` | The chosen provider has no API key |

`GET /healthz/` returns 200 once the database answers and the station data is
loaded, and 503 otherwise. The Docker stack uses it as the container health check.

`GET /api/v1/settings/` returns what the server uses when a request does not
say otherwise: each setting's value, default and description, and for each
routing provider whether it is in use and whether an API key is stored (never
the key itself). Anyone may read it.

`PATCH /api/v1/settings/` changes settings and stores provider keys:

```json
{"settings": {"stops.cost_per_stop": 8, "vehicle.mpg": 12}, "provider_keys": {"openrouteservice": "..."}}
```

Because settings apply to every client, this needs a signed-in account with
permission to change them, and Django's CSRF token in an `X-CSRFToken` header.
Every value is validated and nothing is saved unless all of it is valid; the
server will not switch to a provider that has no key. `POST /api/v1/session/`
with a username and password signs in (the same accounts and session as the
admin, limited to 10 attempts a minute), and `DELETE` signs out. The route
endpoint itself takes no credentials.

A Postman collection with these requests is in
[docs/postman_collection.json](docs/postman_collection.json).

## How it works

1. **Resolve the endpoints offline.** "City, ST" is looked up in a table of
   32,058 US places from the Census Gazetteer. No network call.
2. **Fetch the route.** One call to the routing provider returns the road
   geometry and distance.
3. **Find the stations on the route.** Stations are held in memory as numpy
   arrays. A coarse grid narrows them to the few hundred near the route, then one
   matrix product gives each its distance from the route and its mile marker.
   Stations within 5 miles count.
4. **Choose the stops.** Minimise the fuel bill plus a fixed cost per stop, with
   a dynamic programme over (station, fuel on arrival). With a stop cost of 0 a
   greedy look-ahead gives the cheapest possible bill. See
   `planner/services/optimizer.py`.
5. **Cache.** The finished plan and the provider's route are cached (Redis, or
   in-process without it), so a repeat makes no routing call and returns in
   milliseconds.

The price file has no coordinates. Each station is placed at the centre of its
town: 6,313 from the Census file, and the 313 whose towns the Census does not
list from a one-off Nominatim lookup whose results are committed in
`data/nominatim_cache.json`. The import therefore runs offline.

### Performance

Measured on a laptop against the public OSRM server, with the debug toolbar
off; these vary from run to run.

| | First request | Repeat |
| --- | --- | --- |
| New York to Los Angeles (2,810 mi) | about 680 ms | about 18 ms |
| Chicago to Houston (1,083 mi) | about 330 ms | about 10 ms |

Nearly all of a first request is the routing call. Local work on a
cross-country route is about 12 ms to match stations and 30 ms to optimise.

In the debug toolbar, a plan is three SQL queries taking about 2 ms together:
the settings, and one indexed lookup each for the start and the finish. The
stations are matched in memory, so they cost no query.

## Configuration

Runtime settings are rows in the `Setting` table. Change them in the page's
settings drawer (the gear button) or in the Django admin at `/admin/`; either
way they take effect on the next request.

The Docker stack creates an admin login on first start: **admin** /
**fuelroute-demo**. For a local run, create the same one with:

```bash
python manage.py seed_admin
```

That demo password is published here, so it is only ever used on a local stack:
`seed_admin` refuses it when `DJANGO_DEBUG` is off unless
`DJANGO_SUPERUSER_USERNAME` and `DJANGO_SUPERUSER_PASSWORD` are set, and the
compose file reads the same two variables.

| Key | Default | Meaning |
| --- | --- | --- |
| `routing.provider` | `osrm` | `osrm` or `openrouteservice` |
| `stops.cost_per_stop` | `5` | Dollars one extra stop is worth avoiding |
| `stations.corridor_miles` | `5` | How far from the route a station may be |
| `vehicle.range_miles` | `500` | Distance on a full tank |
| `vehicle.mpg` | `10` | Fuel economy |

Deployment settings come from the environment; `.env.example` lists them all.
The ones that matter beyond a local run:

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL`, `REDIS_URL` | PostgreSQL and Redis. Unset means SQLite and an in-process cache |
| `DJANGO_DEBUG`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` | The usual Django three. The secret key is required when debug is off |
| `DJANGO_SECURE` | `true` behind an HTTPS proxy: redirect to https, HSTS, secure cookies |
| `DJANGO_DEBUG_TOOLBAR` | `true` puts Django Debug Toolbar on every page, for every visitor. For a local stack only: the server refuses to start with it and `DJANGO_SECURE` together |
| `API_RATE_LIMIT` | Requests per client on the route endpoint. Default `120/min`; empty disables it |
| `LOGIN_RATE_LIMIT` | Sign-in attempts per client. Default `10/min` |

### Using OpenRouteService

OSRM's public server needs no key. OpenRouteService needs a free one
([sign up here](https://openrouteservice.org/dev/#/signup)), which is stored
encrypted in the `ProviderCredential` table.

```bash
python manage.py generate_encryption_key       # put the output in .env as CREDENTIALS_ENCRYPTION_KEYS
python manage.py set_provider_key openrouteservice --activate
```

`--activate` also makes it the default provider. The key can be pasted into the
settings drawer or entered in the admin instead; all three need the
encryption key to be set first. Note that the OpenRouteService integration is covered by tests
with mocked responses but has not yet been run against the live API.

## Tests

```bash
python manage.py test                                  # SQLite
docker compose exec web python manage.py test          # PostgreSQL
```

171 tests, 95% line coverage of the Python code. The optimizer is checked against
brute force and an independent formula on random routes. The routing providers
and Nominatim are mocked, so the suite makes no network calls, and it always
uses a private cache.

## Limitations

- Stations are positioned at their town centre, not their exact exit, and the
  detour to reach one is not added to the trip.
- With the default full tank, a trip under 500 miles needs no fuel and costs $0.
- The public OSRM server has no uptime guarantee and routes for cars.
- Canadian stations in the price file are ignored.
- The station index is loaded once per server process; restart the server after
  re-importing data.
- The page's JavaScript has no automated tests. It was checked by driving the
  running page in a headless browser.

## Data sources

- **Fuel prices:** the file supplied with the assignment.
- **US places:** the U.S. Census Bureau 2025 Gazetteer (public domain).
- **Town positions the Census does not list** (`data/nominatim_cache.json`) and
  the map tiles: © OpenStreetMap contributors, under the
  [Open Database License](https://www.openstreetmap.org/copyright), looked up
  through Nominatim.
- **Routing:** the public [OSRM](https://project-osrm.org/) demo server, or
  [OpenRouteService](https://openrouteservice.org/), both built on OpenStreetMap data.

## Layout

```
config/                    Django project (settings, urls, wsgi)
planner/
  models.py                Place, FuelStation, Setting, ProviderCredential
  conf.py                  runtime settings: keys, defaults, validation
  crypto.py                encrypted model field for API keys
  providers/               OSRM and OpenRouteService clients, polyline decoder
  services/
    places.py              offline "City, ST" lookup
    stations.py            in-memory station index, route matching
    optimizer.py           fuel stop selection
    trip.py                puts the pieces together, caching
  management/commands/     data import, credential management
  views.py, serializers.py API, map page, health check
  author.py                the note and links in the page's About drawer
  templates/, static/      the map page: markup, styles, and the script that calls the API
  handlers.py              one JSON shape for every API error
  throttling.py            per-client rate limit
  warmup.py                work done once at server start
data/                      price file, Census Gazetteer, Nominatim cache
docs/                      brief, decision log, Postman collection
```
