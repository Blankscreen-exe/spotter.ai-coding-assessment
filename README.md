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
few seconds. Then open http://localhost:8000/map/.

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
optional ones.

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
- `meta.served_from` is `routing provider`, `route cache` or `plan cache`, and
  `meta.routing_api_calls` is 1 or 0 accordingly.

Errors all have the shape `{"error": {"code": "...", "message": "..."}}`:

| Status | `code` | When |
| --- | --- | --- |
| 400 | `invalid_request` | A field is missing or malformed (`fields` lists them) |
| 400 | `location_not_found` | A place cannot be resolved, or its name is ambiguous without a state |
| 422 | `route_not_found` | No driving route exists between the two points |
| 422 | `no_feasible_fuel_plan` | A stretch of the route has no station within range |
| 429 | `throttled` | More than 60 requests a minute from one client (`Retry-After` says when to retry) |
| 500 | `internal_error` | Anything unexpected; details go to the server log, not the response |
| 502 | `routing_provider_error` | The routing API failed or timed out |
| 503 | `routing_provider_not_configured` | The chosen provider has no API key |

`GET /healthz/` returns 200 once the database answers and the station data is
loaded, and 503 otherwise. The Docker stack uses it as the container health check.

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

Measured on a laptop against the public OSRM server; these vary from run to run.

| | First request | Repeat |
| --- | --- | --- |
| New York to Los Angeles (2,810 mi) | about 680 ms | about 18 ms |
| Chicago to Houston (1,083 mi) | about 330 ms | about 10 ms |

Nearly all of a first request is the routing call. Local work on a
cross-country route is about 12 ms to match stations and 30 ms to optimise.

## Configuration

Runtime settings are rows in the `Setting` table, editable in the Django admin
at `/admin/`. They take effect on the next request. Create an admin user first:

```bash
python manage.py createsuperuser                          # local
docker compose exec web python manage.py createsuperuser  # Docker
```

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
| `API_RATE_LIMIT` | Requests per client on the route endpoint. Default `60/min`; empty disables it |

### Using OpenRouteService

OSRM's public server needs no key. OpenRouteService needs a free one, which is
stored encrypted in the `ProviderCredential` table.

```bash
python manage.py generate_encryption_key       # put the output in .env as CREDENTIALS_ENCRYPTION_KEYS
python manage.py set_provider_key openrouteservice --activate
```

`--activate` also makes it the default provider. The key can be entered in the
admin instead. Note that the OpenRouteService integration is covered by tests
with mocked responses but has not yet been run against the live API.

## Tests

```bash
python manage.py test                                  # SQLite
docker compose exec web python manage.py test          # PostgreSQL
```

114 tests, 94% line coverage. The optimizer is checked against brute force and an
independent formula on random routes. The routing providers and Nominatim are
mocked, so the suite makes no network calls, and it always uses a private cache.

## Limitations

- Stations are positioned at their town centre, not their exact exit, and the
  detour to reach one is not added to the trip.
- With the default full tank, a trip under 500 miles needs no fuel and costs $0.
- The public OSRM server has no uptime guarantee and routes for cars.
- Canadian stations in the price file are ignored.
- The station index is loaded once per server process; restart the server after
  re-importing data.

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
  handlers.py              one JSON shape for every API error
  throttling.py            per-client rate limit
  warmup.py                work done once at server start
data/                      price file, Census Gazetteer, Nominatim cache
docs/                      brief, decision log, Postman collection
```
