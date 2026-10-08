# Fuel route planner

A Django API that takes a start and a finish in the USA and returns the driving
route, where to stop for fuel along it, and what the fuel costs, for a vehicle
with a 500 mile range that does 10 miles per gallon.

## The brief, point by point

| The brief asks for | How it is met | Where it shows |
| --- | --- | --- |
| An API that takes a start and a finish within the USA | `POST /api/v1/route/` with `"City, ST"` or `"lat,lon"` | [The API](#the-api) |
| A map of the route | The route as GeoJSON in the response, and a link to a page that draws it | `route`, `map_url` |
| Optimal places to fuel up, by cost, within a 500 mile range | An exact optimizer over every station in the price file that lies along the route. It minimises the fuel bill plus $5 for each stop, so the plan is not fourteen small top-ups; `stop_cost: 0` gives the lowest possible bill | `fuel_stops` |
| Total money spent on fuel at 10 mpg | The fuel bought on the way, and beside it the cost of all the fuel burned | `summary.total_fuel_cost`, `summary.fuel_used_cost` |
| The supplied fuel price file | All 6,626 US stations in it are loaded and used | `data/` |
| A free map and routing API | The public OSRM server, on OpenStreetMap data | `meta.routing_provider` |
| Latest stable Django | Django 6.1.2 | `requirements.txt` |
| Quick results | A new cross-country trip in 0.3 to 0.9 s, nearly all of it the routing call. A repeat in about 10 ms | [Performance](#performance) |
| One call to the routing API is ideal | One call for a new trip, none for a repeat | `meta.routing_api_calls` |

### Check it in two minutes

```bash
docker compose up --build
curl -X POST http://127.0.0.1:8000/api/v1/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": "Los Angeles, CA", "include_geometry": false}'
```

The answer is 2,810 miles, seven fuel stops and about $710 of fuel, with
`"routing_api_calls": 1` under `meta`. Send it again and that becomes 0. Leave
out `include_geometry` to get the route line, and open `map_url` to see the
plan on a map. The same requests are in a Postman collection,
[docs/postman_collection.json](docs/postman_collection.json), and the reasoning
behind the design is in [docs/DECISIONS.md](docs/DECISIONS.md).

## Run it

**With Docker** (PostgreSQL and Redis): `docker compose up --build`, then
http://127.0.0.1:8000/. The first start runs the migrations and loads the
reference data, which takes about twenty seconds. The stack is published to
this machine only, on `127.0.0.1`. Use that address in curl and Postman, not
`localhost`: some clients try the IPv6 loopback first, and on Windows that
costs curl 0.2 seconds a request.

**Without Docker** (SQLite, no services, no environment variables). Developed
and tested on Python 3.14; Django 6.1 needs 3.12 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py import_places    # US towns from the Census Gazetteer
python manage.py import_stations  # fuel stations from the price file
python manage.py runserver
```

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

The response for `{"start": "Chicago, IL", "finish": "Houston, TX"}`, with the
second stop and the route line shortened:

```json
{
  "start": {"query": "Chicago, IL", "name": "Chicago, IL", "lat": 41.837045, "lon": -87.684939},
  "finish": {"query": "Houston, TX", "name": "Houston, TX", "lat": 29.785743, "lon": -95.388806},
  "summary": {
    "distance_miles": 1083.1, "duration_hours": 19.89, "fuel_stops": 2,
    "total_fuel_cost": 170.02, "gallons_purchased": 58.31,
    "gallons_used": 108.31, "fuel_used_cost": 315.81, "currency": "USD"
  },
  "vehicle": {"max_range_miles": 500.0, "miles_per_gallon": 10.0, "initial_range_miles": 500.0},
  "planning": {"stop_cost": 5.0, "corridor_miles": 5.0},
  "fuel_stops": [
    {
      "order": 1, "station_id": 66643, "name": "DEERFIELD TRAVEL CENTER", "address": "I-55, EXIT 8",
      "city": "Steele", "state": "MO", "lat": 36.09479, "lon": -89.862102,
      "mile_marker": 448.6, "miles_off_route": 2.9, "price_per_gallon": 2.976,
      "gallons_on_arrival": 5.14, "gallons_purchased": 28.84, "cost": 85.8
    },
    {"order": 2, "name": "Quiktrip #7900", "city": "Texarkana", "state": "TX", "mile_marker": 788.4, "cost": 84.22}
  ],
  "route": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-87.68494, 41.83705]]}},
  "meta": {
    "routing_provider": "osrm", "routing_api_calls": 1, "served_from": "routing provider",
    "stations_considered": 158, "elapsed_ms": 254.0
  },
  "map_url": "http://127.0.0.1:8000/map/?start=Chicago%2C+IL&finish=Houston%2C+TX"
}
```

- `total_fuel_cost` is the fuel bought on the way. The vehicle starts full by
  default, so a trip under 500 miles buys nothing and this is 0.
- `fuel_used_cost` counts the starting fuel too: every gallon burned, at the
  plan's average price per gallon, or at the cheapest station on the route when
  nothing is bought. It is `null` only if the route passes no station at all.
- `route` is GeoJSON, at most 3,000 points. `map_url` opens the same plan on an
  interactive map.
- `meta.served_from` is `routing provider`, `route cache` or `plan cache`, and
  `meta.routing_api_calls` is 1 or 0 accordingly.

Every response from the endpoint is JSON, errors included. An error is
`{"error": {"code": "...", "message": "..."}}`:

| Status | `code` | When |
| --- | --- | --- |
| 400 | `invalid_request` | A field is missing or malformed (`fields` lists them) |
| 400 | `parse_error` | The body is not valid JSON |
| 400 | `location_not_found` | A place cannot be resolved, or its name is ambiguous without a state |
| 405 | `method_not_allowed` | Anything but GET or POST |
| 415 | `unsupported_media_type` | A body that is not sent as `application/json` |
| 422 | `route_not_found` | No driving route exists between the two points |
| 422 | `no_feasible_fuel_plan` | A stretch of the route has no station within range |
| 429 | `throttled` | More than 120 requests a minute from one client (`Retry-After` says when to retry) |
| 500 | `internal_error` | Anything unexpected; details go to the server log, not the response |
| 502 | `routing_provider_error` | The routing API failed or timed out |
| 503 | `routing_provider_not_configured` | The chosen provider has no usable API key |

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
   greedy look-ahead gives the cheapest possible bill. Both are checked against
   brute force on random routes. See `planner/services/optimizer.py`.
5. **Cache.** Two things are kept for an hour (in Redis, or in-process without
   it): the route made ready for planning, which is its line thinned for
   drawing plus the stations matched to it, and the stops chosen under each set
   of settings. A repeat reads both in one round trip and makes no routing call.

The price file has no coordinates. Each station is placed at the centre of its
town: 6,313 from the Census file, and the 313 whose towns the Census does not
list from a one-off Nominatim lookup whose results are committed in
`data/nominatim_cache.json`. The import therefore runs offline.

### Performance

Measured on a laptop against the public OSRM server, through the view with
gzip, with the debug toolbar off. The routing call varies from run to run.

| | First request | Repeat | Response, gzipped |
| --- | --- | --- | --- |
| New York to Los Angeles (2,810 mi) | 0.3 to 0.9 s | about 10 ms | 24 KB, or 1 KB without the route line |
| Chicago to Houston (1,083 mi) | about 0.25 s | about 10 ms | 22 KB, or 1 KB |

A plan is three SQL queries taking about 2 ms together: the settings, and one
indexed lookup each for the start and the finish. The stations are matched in
memory, so they cost no query.

On New York to Los Angeles there are n = 349 stations on the route and at most
w = 140 within one tank of each other. Matching the stations takes 15 ms and
choosing the stops 37 ms. The dynamic programme's time grows as n × w × log w
and its memory as n × w; the greedy, used when stops are free, is n × w and
takes 0.2 ms. What bounds it:

- **Stations per route.** The corridor setting cannot be raised past 25 miles,
  where choosing the stops takes 0.16 s against 0.04 s at the default 5.
- **Cache size.** About 110 KB per trip and 1 to 2 KB per plan. The compose
  stack caps Redis at 256 MB and lets it drop its least recently used entries.
- **Routing calls.** One for a new trip, none for a repeat, one for a burst of
  identical new requests. If that call fails, those waiting try one at a time.

## Tests

```bash
python manage.py test                                  # SQLite
docker compose exec web python manage.py test          # PostgreSQL
pip install -r requirements-dev.txt                    # adds the linter and the browser tests
ruff check . && ruff format --check .
```

233 tests, 95% line coverage of the Python code. The routing providers and
Nominatim are mocked, and the suite always uses a private cache. Twenty-six
of the tests drive the map page in a real browser; they need the development
requirements, an installed Chrome or Edge and the network (the page loads
Leaflet from a CDN), and are skipped without them, as in the Docker image. A
GitHub Actions workflow runs the linter and the tests on SQLite and PostgreSQL.

## Limitations

- Stations are positioned at their town centre, not their exact exit, and the
  detour to reach one is not added to the trip. The price file gives no
  coordinates, only a town and an address such as "I-80 Exit 223". The API's
  `lat`, `lon` and `miles_off_route` describe the town centre; the map draws
  each fuel stop at the point of the route nearest its town.
- `"lat,lon"` input is checked against rough boxes around the lower 48 states,
  Alaska and Hawaii, not against the border. A point just outside the country,
  such as Toronto, is accepted and routed like any other.
- A name that fits several places ("Springfield", or "New York", which Florida
  also has) is answered with a 400 that lists them; add the state.
- The public OSRM server has no uptime guarantee and routes for cars. A routing
  call may take up to 20 seconds before it is given up on.
- Canadian stations in the price file are ignored.
- The station index is loaded once per server process; restart the server after
  re-importing data.

## Beyond the brief

The brief asks for one endpoint. Everything in this section is extra, and none
of it is needed to check the points above.

- **A page that uses the API.** `/map/` (where `/` and every `map_url` lead)
  asks where from and where to, then shows the route, the stops and the fuel
  bill. The two location boxes offer places as a name is typed, from
  `GET /api/v1/places/?q=chi`: one indexed query, about 2 ms, returning up to
  eight names such as `{"places": [{"name": "Chicago, IL"}]}`. Two counters
  change the cost per stop and the starting fuel, and the
  trip is planned again. Tabs along the bottom show the fuel in the tank along
  the trip, the plan as a table, neighbouring cost-per-stop settings, the plan
  as GeoJSON for [geojson.io](https://geojson.io/), and the API call behind
  what is on screen. It plans nothing itself: everything on it came from
  `POST /api/v1/route/`.
- **Settings without a deploy.** The routing provider, the vehicle's range and
  fuel economy, the cost per stop and the station corridor are rows in a
  `Setting` table, edited in the page's settings drawer (the gear button) or
  the Django admin. `GET /api/v1/settings/` returns them to anyone. `PATCH`
  changes them and needs a signed-in account with permission and Django's CSRF
  token; `POST /api/v1/session/` signs in with the admin's accounts, at most 10
  attempts a minute.
- **A second routing provider.** OpenRouteService can stand in for OSRM. It
  needs a free key ([sign up here](https://openrouteservice.org/dev/#/signup)),
  which is stored encrypted and never returned by the API. Set it in the
  settings drawer, or with `python manage.py generate_encryption_key` (its
  output goes in `.env` as `CREDENTIALS_ENCRYPTION_KEYS`) and then
  `python manage.py set_provider_key openrouteservice --activate`. It has been
  run against the live API; its error handling is tested with mocked responses.
- **Django Debug Toolbar.** The compose stack starts with it on: the green tab
  on the right edge of every page shows the SQL queries, cache calls and timing
  behind a request, and on the map page it follows the API calls the page
  makes. It adds 25 to 30 ms to each request;
  `DJANGO_DEBUG_TOOLBAR=false docker compose up` runs without it.
- **An admin login for the demo.** The compose stack creates **admin** /
  **fuelroute-demo** on first start, and `python manage.py seed_admin` does the
  same for a local run. That password is published here, which is one reason
  the stack is reachable from this machine only. With `DJANGO_DEBUG` off the
  command never falls back to it.
- **Around the endpoint.** `GET /healthz/` returns 200 once the database
  answers and the station data is loaded, and 503 otherwise. The route endpoint
  allows 120 requests a minute per client address.

Deployment settings come from the environment and `.env.example` lists them
all. Beyond a local run: `DATABASE_URL` and `REDIS_URL` (unset means SQLite and
an in-process cache), `DJANGO_SECRET_KEY` (required when `DJANGO_DEBUG` is
off), `DJANGO_SECURE` behind an HTTPS proxy, and `DJANGO_NUM_PROXIES` so the
rate limits count the real client and not the proxy. The debug toolbar is for
a local stack only: the server refuses to start with it and `DJANGO_SECURE`
together.

## Data sources

Fuel prices: the file supplied with the assignment. US places: the U.S. Census
Bureau 2025 Gazetteer (public domain). Town positions the Census does not list,
and the map tiles: © OpenStreetMap contributors, under the
[Open Database License](https://www.openstreetmap.org/copyright). Routing: the
public [OSRM](https://project-osrm.org/) demo server, or
[OpenRouteService](https://openrouteservice.org/).

## Layout

```
config/                  Django project: settings, urls, wsgi
planner/
  views.py               thin: the API endpoints, the map page, the health check
  serializers.py         what the API accepts and the JSON it returns
  services/              the work itself, with no knowledge of HTTP or JSON
    places.py            offline "City, ST" lookup
    stations.py          in-memory station index, route matching
    optimizer.py         fuel stop selection
    trip.py              plans a trip from those pieces, with caching
    server_settings.py   the runtime settings and provider keys
  providers/             OSRM and OpenRouteService clients
  models.py, conf.py     the four tables, and which runtime settings exist
  management/commands/   data import, credential management
  tests/                 API, services, commands, and the page in a browser
data/, docs/             price file and reference data; brief, decision log, Postman collection
```
