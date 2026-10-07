// The map page. It is a client of the public API and nothing more: every plan
// on screen came from POST /api/v1/route/, the same call a reviewer makes in
// Postman, and the "API call" tab shows it.
(() => {
  'use strict';

  const config = JSON.parse(document.getElementById('planner-config').textContent);
  const DEFAULTS = config.defaults;  // what the server uses when a request does not say otherwise
  const TABS = ['timeline', 'plan', 'tradeoff', 'geojson', 'api', 'server'];
  const EXAMPLES = [
    { label: 'Coast to coast', start: 'New York, NY', finish: 'Los Angeles, CA' },
    { label: 'Midwest to Gulf', start: 'Chicago, IL', finish: 'Houston, TX' },
    { label: 'Short hop on a quarter tank', start: 'Dallas, TX', finish: 'Austin, TX', fuel: DEFAULTS.rangeMiles / 4 },
    { label: 'Ambiguous place', start: 'Springfield', finish: 'Miami, FL' },
  ];

  const $ = (id) => document.getElementById(id);
  // Station names and error messages come from data and from what the user typed; never trust them as HTML.
  const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (value) => '$' + Number(value).toFixed(2);
  const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

  let current = null;   // the plan on screen: { request, status, body }
  let lastCall = null;  // the most recent call, successful or not, for the API tab
  let previous = null;  // summary of the plan shown before this one, for the same trip
  let shownTrip = null;
  let sequence = 0;
  let health = null;
  // The bottom drawer starts closed, showing only the numbers and the tab names.
  // A link that names a tab (…#plan) opens it on that tab.
  let tab = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'timeline';
  let dockOpen = TABS.includes(location.hash.slice(1));
  const comparisons = new Map();  // trip and starting fuel -> plans fetched for the "Stops against cost" tab, by cost
  let geoWithStations = false;    // whether the GeoJSON tab also lists the stations passed over

  // ---------- talking to the API ----------

  async function callApi(request) {
    let response;
    try {
      response = await fetch(config.apiUrl, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
      });
    } catch (error) {
      return { request, status: 0, body: { error: { code: 'network_error', message: 'Could not reach the server.' } } };
    }
    let body;
    try {
      body = await response.json();
    } catch (error) {
      body = { error: { code: 'bad_response', message: `The server answered HTTP ${response.status} without JSON.` } };
    }
    return { request, status: response.status, body };
  }

  // Leaving a number out lets the server use its own setting. The page also asks for the stations the
  // planner chose from, to draw them; a plain API call is not sent them unless it asks.
  // provider is only ever what a link named (the API's map_url repeats it); left out, the server's setting decides.
  function buildRequest(start, finish, cost, fuel, provider) {
    const request = { start, finish, include_candidates: true };
    if (cost !== undefined) request.stop_cost = cost;
    if (fuel !== undefined) request.initial_range_miles = fuel;
    if (provider) request.provider = provider;
    return request;
  }

  function errorHtml(call) {
    const error = call.body.error || { code: 'error', message: 'Something went wrong.' };
    const fields = error.fields ? ' ' + Object.entries(error.fields).map(([name, problems]) => `${name}: ${[].concat(problems).join(' ')}`).join('; ') : '';
    return `<div class="error"><b>${esc(error.message + fields)}</b><br><code>HTTP ${esc(call.status)} &middot; ${esc(error.code)}</code></div>`;
  }

  // ---------- map ----------

  // The page has its own zoom buttons in the trip bar, so the map's corner control is switched off.
  const map = L.map('map', { zoomControl: false }).setView([39.5, -98.35], 4);
  $('zoomIn').addEventListener('click', () => map.zoomIn());
  $('zoomOut').addEventListener('click', () => map.zoomOut());
  const syncZoomButtons = () => {
    $('zoomIn').disabled = map.getZoom() >= map.getMaxZoom();
    $('zoomOut').disabled = map.getZoom() <= map.getMinZoom();
  };
  map.on('zoomend', syncZoomButtons);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    // OpenStreetMap's tile policy requires a Referer and serves "Access blocked"
    // tiles without one. Django's default Referrer-Policy (same-origin) withholds
    // it from other sites, so the tile requests opt back in, sending the origin only.
    referrerPolicy: 'strict-origin-when-cross-origin',
  }).addTo(map);
  const tripLayer = L.layerGroup().addTo(map);
  const otherStops = L.layerGroup().addTo(map);  // where the neighbouring plans would stop
  // The stations the planner passed over are drawn as dots on a canvas, which copes with hundreds of them.
  // A dot is small, so the pointer counts as on it from a few pixels away. The canvas has a layer of its
  // own, above the route line (which would otherwise cover the dots) and below the numbered stops.
  map.createPane('stations').style.zIndex = 450;
  const dots = L.canvas({ pane: 'stations', padding: 0.5, tolerance: 4 });
  let passedOver = [];
  // Seen from far out the dots all sit on the route line, so they shrink to a hint; closer in they grow.
  const dotSize = () => (map.getZoom() <= 5 ? 2.5 : map.getZoom() <= 7 ? 3.5 : 4.5);
  map.on('zoomend', () => passedOver.forEach((dot) => dot.setRadius(dotSize())));
  syncZoomButtons();  // now that the tile layer has told the map how far it can zoom

  function pin(lat, lon, html, size) {
    return L.marker([lat, lon], { icon: L.divIcon({ className: '', html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] }) });
  }

  // The finish is a chequered flag. It is drawn here rather than typed as the emoji, which looks different on
  // every system and has no exact point to stand on: the foot of this pole is the destination itself.
  const FLAG = `<svg class="flag" viewBox="0 0 30 32" width="30" height="32" aria-hidden="true">
    <path d="M5 3v26" stroke="#fff" stroke-width="5.5" stroke-linecap="round"/>
    <rect x="5.5" y="2.5" width="23" height="18" rx="2" fill="#fff"/>
    <path d="M7 4h5v5h-5zM17 4h5v5h-5zM12 9h5v5h-5zM22 9h5v5h-5zM7 14h5v5h-5zM17 14h5v5h-5z" fill="currentColor"/>
    <rect x="7" y="4" width="20" height="15" fill="none" stroke="currentColor"/>
    <path d="M5 3v26" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
    <circle class="foot" cx="5" cy="29" r="3" stroke="#fff" stroke-width="1.5"/>
  </svg>`;

  function flag(lat, lon) {
    return L.marker([lat, lon], { icon: L.divIcon({ className: '', html: FLAG, iconSize: [30, 32], iconAnchor: [5, 29], popupAnchor: [0, -28] }) });
  }

  function popup(title, lines) {
    const box = document.createElement('div');
    const head = document.createElement('b');
    head.textContent = title;
    box.append(head);
    for (const line of lines) { const row = document.createElement('div'); row.textContent = line; box.append(row); }
    return box;
  }

  // Where a fuel stop is drawn. The price file gives a station's town, not its position, so the data places
  // it at the centre of that town, which can be a few miles from the road. The stations themselves are at
  // exits on the route, so a stop is drawn at the point of the route nearest its town, and sits on the line.
  // The stations passed over stay at their towns' centres: scattered beside the route, they show the band
  // of country the planner chose from. Only the drawing moves: the API's own numbers are untouched.
  let snappedTo = '';          // which route the answers below belong to
  const snapped = new Map();   // "lat,lon" of a town -> [lat, lon] on that route
  function onRoute(body, place) {
    const line = body.route ? body.route.geometry.coordinates : [];
    if (line.length < 2) return [place.lat, place.lon];
    const route = [line.length, line[0], line[line.length >> 1], line[line.length - 1]].join('|');
    if (route !== snappedTo) {
      snappedTo = route;
      snapped.clear();
    }
    const town = `${place.lat},${place.lon}`;
    if (!snapped.has(town)) snapped.set(town, nearestOn(line, place.lat, place.lon));
    return snapped.get(town);
  }

  // The point of a line ([[lon, lat], ...]) nearest to a place. Distances are judged on a flat sheet scaled
  // for the place's latitude, which is exact enough over the few miles involved.
  function nearestOn(line, lat, lon) {
    const squash = Math.cos(lat * Math.PI / 180);
    let least = Infinity, nearest = [lat, lon];
    for (let i = 1; i < line.length; i++) {
      // Both ends of this stretch, measured from the place.
      const ax = (line[i - 1][0] - lon) * squash, ay = line[i - 1][1] - lat;
      const dx = (line[i][0] - lon) * squash - ax, dy = line[i][1] - lat - ay;
      const length = dx * dx + dy * dy;
      const along = length ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / length)) : 0;
      const x = ax + along * dx, y = ay + along * dy;
      if (x * x + y * y < least) {
        least = x * x + y * y;
        nearest = [lat + y, lon + x / squash];
      }
    }
    return nearest;
  }

  function drawTrip(body) {
    tripLayer.clearLayers();
    const line = L.geoJSON(body.route, { style: { color: '#1d4ed8', weight: 4 } }).addTo(tripLayer);
    pin(body.start.lat, body.start.lon, '<div class="pin end start" style="width:18px;height:18px"></div>', 18)
      .bindPopup(popup('Start', [body.start.name])).addTo(tripLayer);
    flag(body.finish.lat, body.finish.lon)
      .bindPopup(popup('Finish', [body.finish.name])).addTo(tripLayer);
    // The stations the planner chose from and passed over, as orange dots, so the choice can be seen. Stations
    // are placed at their town's centre, so a town's stations share a spot: one dot for the town, listing them.
    const chosen = new Set(body.fuel_stops.map((stop) => stop.station_id));
    const towns = new Map();
    for (const station of body.candidate_stations || []) {
      if (chosen.has(station.station_id)) continue;
      const spot = `${station.lat},${station.lon}`;
      if (!towns.has(spot)) towns.set(spot, []);
      towns.get(spot).push(station);
    }
    passedOver = [...towns.values()].map((stations) => {
      const [first] = stations.sort((a, b) => a.price_per_gallon - b.price_per_gallon);
      return L.circleMarker([first.lat, first.lon], { renderer: dots, radius: dotSize(), weight: 1, color: '#fff', fillColor: '#f97316', fillOpacity: 0.95 })
        .bindTooltip(() => popup(`${first.city}, ${first.state}`, [
          `Mile ${Math.round(first.mile_marker)}. Considered, not chosen:`,
          ...stations.slice(0, 5).map((station) => `${station.name}, $${station.price_per_gallon.toFixed(3)} a gallon`),
          ...(stations.length > 5 ? [`and ${stations.length - 5} more`] : []),
        ])).addTo(tripLayer);
    });
    for (const stop of body.fuel_stops) {
      const order = Number(stop.order);
      pin(...onRoute(body, stop), `<div class="pin" data-order="${order}" data-station="${Number(stop.station_id)}" style="width:28px;height:28px">${order}</div>`, 28)
        .bindPopup(popup(`${order}. ${stop.name}`, [
          `${stop.city}, ${stop.state}, mile ${Math.round(stop.mile_marker)}`,
          `${stop.gallons_purchased.toFixed(1)} gal at $${stop.price_per_gallon.toFixed(3)} = ${money(stop.cost)}`,
          ...(stop.miles_off_route >= 0.1 ? [`Drawn where the route passes it. The town's centre is ${stop.miles_off_route} mi away.`] : []),
        ])).addTo(tripLayer);
    }
    routeBounds = line.getBounds();
    map.invalidateSize();
    // A new trip is framed whole. A change on a counter keeps whatever view the user has chosen.
    const trip = `${body.start.name}|${body.finish.name}`;
    if (trip !== shownTrip) {
      shownTrip = trip;
      following = true;
    }
    fitRoute();
  }

  // Keep the whole route in view, through window resizes too, until the user moves the map themselves.
  let routeBounds = null, following = true, fitting = false;
  function fitRoute() {
    if (!routeBounds || !following) return;
    fitting = true;
    map.fitBounds(routeBounds, { paddingTopLeft: [30, 70], paddingBottomRight: [30, 30], animate: false });
    fitting = false;
  }
  map.on('resize', fitRoute);
  map.on('movestart', () => { if (!fitting) following = false; });  // a drag, zoom or scroll by the user

  // While "Stops against cost" is open, the map also shows where the other plans in the table would stop:
  // a faint ring for each stop that is not one of this plan's.
  function drawOtherStops() {
    otherStops.clearLayers();
    const known = current && dockOpen && tab === 'tradeoff' && comparisons.get(comparisonKey(current));
    if (!known) return;
    const mine = new Set(current.body.fuel_stops.map((stop) => stop.station_id));
    const others = new Map();  // station id -> the stop, and the settings whose plans use it
    for (const cost of comparedCosts(current.body.planning.stop_cost)) {
      for (const stop of known.plans.has(cost) ? known.plans.get(cost).stops : []) {
        if (mine.has(stop.station_id)) continue;
        if (!others.has(stop.station_id)) others.set(stop.station_id, { stop, costs: [] });
        others.get(stop.station_id).costs.push('$' + Number(cost));
      }
    }
    for (const { stop, costs } of others.values()) {
      pin(...onRoute(current.body, stop), `<div class="ghost" data-station="${Number(stop.station_id)}"></div>`, 16)
        .bindTooltip(popup(stop.name, [
          `${stop.city}, ${stop.state}, mile ${Math.round(stop.mile_marker)}`,
          `$${stop.price_per_gallon.toFixed(3)} a gallon. A stop when one costs ${costs.join(' or ')}.`,
        ])).addTo(otherStops);
    }
  }

  // Pointing at a row of that table picks out the row's plan on the map: its stops stay as they are and
  // every other stop fades. null puts the map back.
  function pickOutPlan(cost) {
    const known = current && comparisons.get(comparisonKey(current));
    const plan = known && cost !== null && known.plans.get(cost);
    document.body.classList.toggle('comparing', Boolean(plan));
    const stops = new Set(plan ? plan.stops.map((stop) => String(stop.station_id)) : []);
    document.querySelectorAll('[data-station]').forEach((node) => node.classList.toggle('in-plan', stops.has(node.dataset.station)));
  }

  // ---------- fuel timeline ----------

  // Fuel in the tank along the trip, in gallons, as [mile, gallons] with a vertical jump at each stop.
  function fuelLevels(body) {
    const mpg = body.vehicle.miles_per_gallon;
    let mile = 0, gallons = body.vehicle.initial_range_miles / mpg;
    const points = [[mile, gallons]];
    for (const stop of body.fuel_stops) {
      points.push([stop.mile_marker, stop.gallons_on_arrival]);
      gallons = stop.gallons_on_arrival + stop.gallons_purchased;
      mile = stop.mile_marker;
      points.push([mile, gallons]);
    }
    points.push([body.summary.distance_miles, Math.max(0, gallons - (body.summary.distance_miles - mile) / mpg)]);
    return points;
  }

  function drawChart(body) {
    const svg = $('chart');
    if (!svg) return;
    const box = svg.getBoundingClientRect();
    const W = box.width || 1000, H = box.height || 220;
    // In a low panel the place names under the axis are left out (the trip bar already says them),
    // which gives the fuel line itself more of the height.
    const compact = H < 170;
    const left = 44, right = 20, top = 28, bottom = compact ? 20 : 38;
    const tank = body.vehicle.max_range_miles / body.vehicle.miles_per_gallon;
    const D = Math.max(body.summary.distance_miles, 0.1);
    const x = (mile) => left + (mile / D) * (W - left - right);
    const y = (gallons) => top + (1 - Math.min(gallons, tank) / tank) * (H - top - bottom);
    const line = fuelLevels(body).map(([mile, gallons]) => `${x(mile).toFixed(1)},${y(gallons).toFixed(1)}`).join(' ');
    const crowded = body.fuel_stops.length > 9;
    let out = `<line x1="${left}" x2="${W - right}" y1="${y(tank)}" y2="${y(tank)}" stroke="#d9dee3" stroke-dasharray="4 4"/>
      <line x1="${left}" x2="${W - right}" y1="${y(0)}" y2="${y(0)}" stroke="#9aa5b1"/>
      <text x="${left - 6}" y="${y(tank) + 4}" text-anchor="end">full</text>
      <text x="${left - 6}" y="${y(0) + 4}" text-anchor="end">empty</text>
      <polygon points="${x(0)},${y(0)} ${line} ${x(D)},${y(0)}" fill="#e8efff"/>
      <polyline points="${line}" fill="none" stroke="#1d4ed8" stroke-width="2.5" stroke-linejoin="round"/>
      ${compact ? '' : `<text x="${x(0)}" y="${H - 6}" text-anchor="start">${esc(body.start.name)}</text>
      <text x="${x(D)}" y="${H - 6}" text-anchor="end">${esc(body.finish.name)} &middot; mile ${Math.round(D).toLocaleString()}</text>`}`;
    for (const stop of body.fuel_stops) {
      const sx = x(stop.mile_marker), order = Number(stop.order);
      out += `<g class="stopmark" data-order="${order}">
        <title>${esc(stop.name)}, ${esc(stop.city)}, ${esc(stop.state)}: ${stop.gallons_purchased.toFixed(1)} gal at $${stop.price_per_gallon.toFixed(3)} = ${money(stop.cost)}</title>
        <rect x="${sx - 12}" y="0" width="24" height="${H}" fill="transparent"/>
        <line x1="${sx}" x2="${sx}" y1="${top - 6}" y2="${y(0)}" stroke="#1d4ed8" stroke-opacity=".25"/>
        <circle cx="${sx}" cy="${top - 14}" r="9" fill="#fff" stroke="#1d4ed8" stroke-width="2"/>
        <text class="n" x="${sx}" y="${top - 10}" text-anchor="middle">${order}</text>
        ${crowded ? '' : `<text class="price" x="${sx}" y="${y(0) + 14}" text-anchor="middle">$${stop.price_per_gallon.toFixed(2)}</text>`}
      </g>`;
    }
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.innerHTML = out;
  }

  // ---------- the tabbed panel ----------

  const comparisonKey = (call) => `${call.body.start.name}|${call.body.finish.name}|${call.body.vehicle.initial_range_miles}`;

  // The plan as a GeoJSON FeatureCollection: the route line, its two ends, each fuel stop and, if asked,
  // the stations passed over. The colour and symbol properties are the "simplestyle" ones that geojson.io
  // and GitHub draw from, so it looks there much as it does here.
  function tripGeoJSON(body, withPassedOver) {
    const point = (place, properties) => ({
      type: 'Feature', properties, geometry: { type: 'Point', coordinates: [place.lon, place.lat] },
    });
    // A fuel stop goes where the map draws it, on the route, so that it sits on the line elsewhere too.
    // Where the data has it, and how far that is from the route, go along as properties.
    const station = (place, properties) => {
      const [lat, lon] = onRoute(body, place);
      return point({ lat: Number(lat.toFixed(5)), lon: Number(lon.toFixed(5)) }, {
        ...properties, miles_off_route: place.miles_off_route, town_centre: [place.lon, place.lat],
      });
    };
    const chosen = new Set(body.fuel_stops.map((stop) => stop.station_id));
    const passedOver = withPassedOver ? (body.candidate_stations || []).filter((other) => !chosen.has(other.station_id)) : [];
    return {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: {
            name: `${body.start.name} to ${body.finish.name}`,
            distance_miles: body.summary.distance_miles,
            duration_hours: body.summary.duration_hours,
            fuel_stops: body.summary.fuel_stops,
            total_fuel_cost: body.summary.total_fuel_cost,
            stroke: '#1d4ed8',
            'stroke-width': 4,
          },
          geometry: body.route.geometry,
        },
        point(body.start, { role: 'start', name: body.start.name, 'marker-color': '#15803d' }),
        ...body.fuel_stops.map((stop) => station(stop, {
          role: 'fuel stop',
          order: stop.order,
          name: stop.name,
          address: stop.address,
          city: stop.city,
          state: stop.state,
          mile_marker: stop.mile_marker,
          price_per_gallon: stop.price_per_gallon,
          gallons_purchased: stop.gallons_purchased,
          cost: stop.cost,
          'marker-color': '#1d4ed8',
          ...(stop.order <= 9 ? { 'marker-symbol': String(stop.order) } : {}),  // the symbols stop at 9
        })),
        point(body.finish, { role: 'finish', name: body.finish.name, 'marker-color': '#b91c1c' }),
        ...passedOver.map((other) => point(other, {
          role: 'considered, not chosen',
          name: other.name,
          city: other.city,
          state: other.state,
          mile_marker: other.mile_marker,
          miles_off_route: other.miles_off_route,
          price_per_gallon: other.price_per_gallon,
          'marker-color': '#f97316',
          'marker-size': 'small',
        })),
      ],
    };
  }

  // One feature to a line: still valid JSON, and the long route line does not bury the rest.
  const geoText = (collection) => '{"type":"FeatureCollection","features":[\n'
    + collection.features.map((feature) => JSON.stringify(feature)).join(',\n') + '\n]}';

  const PANELS = {
    timeline: () => '<svg id="chart" role="img" aria-label="Fuel in the tank along the trip"></svg>',

    plan: (body) => {
      if (!body.fuel_stops.length) return '<p class="note">No fuel stop needed: the trip fits in the starting tank.</p>';
      return '<table><tr><th>#</th><th>Station</th><th>Town</th><th class="num">Mile</th><th class="num">Price / gal</th><th class="num">Gallons</th><th class="num">Cost</th></tr>' +
        body.fuel_stops.map((s) => `<tr class="stoprow" data-order="${Number(s.order)}"><td>${Number(s.order)}</td><td>${esc(s.name)}</td>
          <td>${esc(s.city)}, ${esc(s.state)}</td><td class="num">${Math.round(s.mile_marker)}</td><td class="num">$${s.price_per_gallon.toFixed(3)}</td>
          <td class="num">${s.gallons_purchased.toFixed(1)}</td><td class="num">${money(s.cost)}</td></tr>`).join('') +
        `<tr class="total"><td colspan="5">Total</td><td class="num">${body.summary.gallons_purchased.toFixed(1)}</td><td class="num">${money(body.summary.total_fuel_cost)}</td></tr></table>`;
    },

    // Five plans: the cost per stop on the counter, with the two settings below it and the two above.
    tradeoff: (body) => {
      const key = comparisonKey(current), selected = body.planning.stop_cost;
      if (!comparisons.has(key)) comparisons.set(key, { plans: new Map() });
      const known = comparisons.get(key);
      if (known.failed) return errorHtml(known.failed);
      known.plans.set(selected, { summary: body.summary, stops: body.fuel_stops });  // the plan on screen is one of the five
      const costs = comparedCosts(selected);
      if (costs.some((cost) => !known.plans.has(cost))) loadComparison(current, costs);
      const cheapest = Math.min(...costs.filter((cost) => known.plans.has(cost)).map((cost) => known.plans.get(cost).summary.total_fuel_cost));
      return '<p class="note">This trip at your cost per stop and at the two settings either side of it. Click a row to use it. ' +
        'The rings on the map are stops the other plans would make; point at a row to pick out its plan.</p>' +
        '<table><tr><th>Cost per stop</th><th class="num">Stops</th><th class="num">Fuel bill</th><th class="num">Over the cheapest</th></tr>' +
        costs.map((cost) => {
          const summary = known.plans.has(cost) && known.plans.get(cost).summary;
          const tag = (cost === selected ? ' (selected)' : '') + (cost === DEFAULTS.stopCost ? ' (server default)' : '');
          const start = `<tr class="pick ${cost === selected ? 'current' : ''}" data-cost="${Number(cost)}"><td>$${Number(cost)}${tag}</td>`;
          // A row still on its way keeps its place, so the table does not jump as the counter moves.
          if (!summary) return start + '<td class="num">&hellip;</td><td class="num">&hellip;</td><td class="num">&hellip;</td></tr>';
          const over = summary.total_fuel_cost - cheapest;
          return start + `<td class="num">${Number(summary.fuel_stops)}</td><td class="num">${money(summary.total_fuel_cost)}</td>
            <td class="num">${over < 0.005 ? '&ndash;' : '+' + money(over)}</td></tr>`;
        }).join('') + '</table>';
    },

    // The plan as GeoJSON, to copy into geojson.io or any other map tool. Built from elements, not markup.
    geojson: (body) => {
      const collection = tripGeoJSON(body, geoWithStations);
      const text = geoText(collection);
      const make = (tag, properties) => Object.assign(document.createElement(tag), properties);

      const copy = make('button', { type: 'button', className: 'secondary', textContent: 'Copy' });
      const tick = make('input', { type: 'checkbox', checked: geoWithStations });
      const choice = make('label');
      choice.append(tick, ' Include the stations passed over');
      const note = make('span', { className: 'note' });
      note.append(
        `${collection.features.length} features, ${Math.max(1, Math.round(text.length / 1024))} KB. Paste it into `,
        make('a', { href: 'https://geojson.io/', target: '_blank', rel: 'noopener', textContent: 'geojson.io' }),
        '.',
      );
      const area = make('textarea', { readOnly: true, spellcheck: false, value: text });
      area.setAttribute('aria-label', 'The plan as GeoJSON');

      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch (error) {
          area.select();  // no clipboard access (an insecure address, say): fall back to the old way
          document.execCommand('copy');
        }
        copy.textContent = 'Copied';
        setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
      });
      tick.addEventListener('change', () => {
        geoWithStations = tick.checked;
        showPanel();
      });
      area.addEventListener('focus', () => area.select());

      const bar = make('div', { className: 'output-bar' });
      bar.append(copy, choice, note);
      const box = make('div', { className: 'output' });
      box.append(bar, area);
      return box;
    },

    // Built with textContent so that nothing in a response can be read as HTML.
    api: () => {
      const call = lastCall;
      const body = call.status === 200 && call.body.route
        ? { ...call.body, route: `GeoJSON LineString with ${call.body.route.geometry.coordinates.length} points (left out of this panel)` }
        : call.body;
      const url = new URL(config.apiUrl, location.href).href;
      const box = document.createElement('div');
      const block = (title, text) => {
        const head = document.createElement('h3'), pre = document.createElement('pre');
        head.textContent = title;
        pre.textContent = text;
        box.append(head, pre);
      };
      // Inside single quotes a shell reads everything as it stands except a single quote: "Coeur d'Alene, ID".
      const json = JSON.stringify(call.request).replaceAll("'", "'\\''");
      block('Request', `curl -X POST ${url} \\\n  -H "Content-Type: application/json" \\\n  -d '${json}'`);
      block(`Response: HTTP ${call.status}`, JSON.stringify(body, null, 2));
      return box;
    },

    server: (body) => `<dl class="facts">
        <dt>Health</dt><dd>${health ? esc(health.status) : 'unknown'} (GET ${esc(config.healthUrl)})</dd>
        <dt>Stations loaded</dt><dd>${health && health.stations ? Number(health.stations).toLocaleString() : 'unknown'}</dd>
        <dt>This plan</dt><dd>served from ${esc(body.meta.served_from)}, ${plural(Number(body.meta.routing_api_calls), 'routing call')}, ${Number(body.meta.elapsed_ms)} ms on the server</dd>
        <dt>Stations on this route (the orange dots)</dt><dd>${Number(body.meta.stations_considered)}</dd>
        <dt>Routing provider</dt><dd>${esc(body.meta.routing_provider)}</dd>
        <dt>Vehicle</dt><dd>${Number(body.vehicle.max_range_miles)} mile range, ${Number(body.vehicle.miles_per_gallon)} miles per gallon</dd>
      </dl>
      <p class="note">The routing provider, range, miles per gallon and the default cost per stop are rows in a settings table.
      The gear button (top right) shows them; a change there is picked up by the next request.</p>`,
  };

  function showPanel() {
    document.body.classList.toggle('dock-open', dockOpen);
    document.querySelectorAll('.tab').forEach((button) => {
      const shown = dockOpen && button.dataset.tab === tab;
      button.classList.toggle('on', shown);
      button.setAttribute('aria-expanded', shown);
    });
    $('collapse').hidden = !dockOpen;
    pickOutPlan(null);
    if (!dockOpen) {  // what the panel last held stays there while it slides shut
      drawOtherStops();
      return;
    }
    const content = PANELS[tab](current.body);
    if (typeof content === 'string') $('panel').innerHTML = content;
    else $('panel').replaceChildren(content);
    if (tab === 'timeline') drawChart(current.body);
    if (tab === 'tradeoff') showSelectedRow();
    drawOtherStops();
  }

  // The drawer is short, so the comparison scrolls inside it. The selected row is kept in the middle of
  // what can be seen, under the column names, which stay put at the top.
  function showSelectedRow() {
    const panel = $('panel'), row = panel.querySelector('tr.current'), names = panel.querySelector('th');
    if (!row) return;
    const from = row.getBoundingClientRect().top - panel.getBoundingClientRect().top;
    const to = names.offsetHeight + (panel.clientHeight - names.offsetHeight - row.offsetHeight) / 2;
    panel.scrollTop += from - to;
  }

  // The address keeps the trip, and the tab only while the drawer is open.
  function rememberTab() {
    history.replaceState(null, '', location.pathname + location.search + (dockOpen ? '#' + tab : ''));
  }

  // The cost per stop on screen with the two settings below it and the two above: what one or two presses of
  // either arrow would give. At an end of the counter's range the five slide along, so there are still five.
  function comparedCosts(selected) {
    const reach = (direction) => {
      const found = [];
      for (let at = selected; found.length < 4;) {
        const next = stepFrom(at, direction, costLimits());
        if (next === at) break;
        found.push(at = next);
      }
      return found;
    };
    const below = reach(-1), above = reach(1);
    const fromBelow = Math.min(below.length, Math.max(2, 4 - above.length));
    const fromAbove = Math.min(above.length, 4 - fromBelow);
    return [...below.slice(0, fromBelow).reverse(), selected, ...above.slice(0, fromAbove)];
  }

  // One small request per row not fetched yet: four when the tab is first opened, then one for each step of
  // the counter. The route is already cached, so none of these reaches the routing provider.
  async function loadComparison(call, costs) {
    const key = comparisonKey(call), known = comparisons.get(key);
    if (known.loading) return;  // it shows the panel again when done, which asks for whatever is still missing
    known.loading = true;
    for (const cost of costs.filter((each) => !known.plans.has(each))) {
      const answer = await callApi({
        ...buildRequest(call.request.start, call.request.finish, cost, call.body.vehicle.initial_range_miles, call.request.provider),
        include_candidates: false, include_geometry: false,
      });
      if (answer.status !== 200) {
        known.failed = answer;
        setTimeout(() => { delete known.failed; }, 5000);  // allow a retry, for instance once a rate limit has passed
        break;
      }
      known.plans.set(cost, { summary: answer.body.summary, stops: answer.body.fuel_stops });
    }
    known.loading = false;
    if (tab === 'tradeoff' && current && comparisonKey(current) === key) showPanel();
  }

  // ---------- showing a plan ----------

  function servedSentence(meta) {
    if (meta.served_from === 'routing provider') return `First request for this trip: one call to the routing server (${meta.elapsed_ms} ms).`;
    if (meta.served_from === 'route cache') return `Same route as before, so no routing call. Only the stops were recomputed (${meta.elapsed_ms} ms).`;
    return `Identical request: answered straight from cache (${meta.elapsed_ms} ms).`;
  }

  function deltaSentence(before, after) {
    if (!before) return '';
    const stops = after.fuel_stops - before.fuel_stops, cost = after.total_fuel_cost - before.total_fuel_cost;
    if (!stops && Math.abs(cost) < 0.005) return '';
    const signed = (n, text) => (n > 0 ? '+' : n < 0 ? '−' : '') + text;
    const stopsPart = stops ? signed(stops, plural(Math.abs(stops), 'stop')) : 'the same stops';
    return ` Against your last plan: ${stopsPart}, ${signed(cost, money(Math.abs(cost)))} fuel.`;
  }

  function render(call) {
    const body = call.body;
    const sameTrip = current && current.body.start.name === body.start.name && current.body.finish.name === body.finish.name;
    previous = sameTrip ? current.body.summary : null;
    current = call;

    document.body.classList.remove('empty');
    $('tripName').innerHTML = `${esc(body.start.name)} <span class="arrow">&rarr;</span> ${esc(body.finish.name)}`;
    $('sumCost').textContent = money(body.summary.total_fuel_cost);
    $('sumStops').textContent = body.summary.fuel_stops;
    $('sumMiles').textContent = Math.round(body.summary.distance_miles).toLocaleString();
    $('sumGallons').textContent = body.summary.gallons_purchased.toFixed(1);
    cost.set({ ...costLimits(), value: body.planning.stop_cost });
    // The range is the server's to say; it may have been changed since the page loaded.
    fuel.set({ ...fuelLimits(body.vehicle.max_range_miles), value: body.vehicle.initial_range_miles });
    $('say').textContent = servedSentence(body.meta) + deltaSentence(previous, body.summary);

    drawTrip(body);
    showPanel();
    const query = new URLSearchParams(call.request);
    query.delete('include_geometry');
    query.delete('include_candidates');
    history.replaceState(null, '', `${location.pathname}?${query}${dockOpen ? '#' + tab : ''}`);
  }

  async function replan() {
    clearTimeout(planTimer);
    const ticket = ++sequence;
    $('strip').classList.add('busy');
    const call = await callApi(buildRequest(current.request.start, current.request.finish, cost.value, fuel.value, current.request.provider));
    if (ticket !== sequence) return;  // a newer change is already on its way
    $('strip').classList.remove('busy');
    lastCall = call;
    if (call.status === 200) { render(call); return; }
    // Keep the plan that is on screen and say why the new one could not be made.
    $('say').innerHTML = errorHtml(call);
    cost.set({ value: current.body.planning.stop_cost });
    fuel.set({ value: current.body.vehicle.initial_range_miles });
    if (tab === 'api') showPanel();
  }

  // ---------- onboarding ----------

  function show(step) {
    for (const id of ['askStart', 'askFinish', 'working']) $(id).hidden = id !== step;
    $('dot2').classList.toggle('on', step !== 'askStart');
    if (step === 'askStart') $('start').focus();
    if (step === 'askFinish') $('finish').focus();
  }

  function openOnboarding() {
    $('onboarding').hidden = false;
    $('cancel').hidden = !current;
    $('startError').innerHTML = $('finishError').innerHTML = '';
    show('askStart');
  }

  async function firstPlan(request) {
    $('finishError').innerHTML = '';
    $('fromName').textContent = request.start;
    show('working');
    sequence++;
    const call = await callApi(request);
    lastCall = call;
    if (call.status !== 200) {
      // The API's own message says what to fix, so show it as it is.
      show('askFinish');
      $('finishError').innerHTML = errorHtml(call);
      return;
    }
    $('onboarding').hidden = true;
    render(call);
  }

  $('examples').innerHTML = EXAMPLES.map((example, index) => `<button type="button" class="chip" data-example="${index}">${esc(example.label)}</button>`).join('');
  $('examples').addEventListener('click', (event) => {
    const example = EXAMPLES[event.target.dataset.example];
    if (!example) return;
    $('start').value = example.start;
    $('finish').value = example.finish;
    firstPlan(buildRequest(example.start, example.finish, undefined, example.fuel));
  });
  $('askStart').addEventListener('submit', (event) => {
    event.preventDefault();
    const start = $('start').value.trim();
    if (!start) { $('startError').innerHTML = '<div class="error">Tell me where you are starting from.</div>'; return; }
    $('startError').innerHTML = '';
    $('fromName').textContent = start;
    show('askFinish');
  });
  $('askFinish').addEventListener('submit', (event) => {
    event.preventDefault();
    const finish = $('finish').value.trim();
    if (!finish) { $('finishError').innerHTML = '<div class="error">Tell me where you are going.</div>'; return; }
    firstPlan(buildRequest($('start').value.trim(), finish));
  });
  $('back').addEventListener('click', () => show('askStart'));
  $('cancel').addEventListener('click', () => { $('onboarding').hidden = true; });
  $('change').addEventListener('click', openOnboarding);

  // ---------- controls ----------

  // What the two counters offer. The API takes more than this (any cost from $0 up, any fuel up to a full
  // tank); these are the values worth trying by hand. Whatever the server is set to is always within reach.
  //   Cost per stop, $1 to $45. Stopping always costs some time, and $45 is about the most it can: half an
  //   hour off the road at the $91 or so an hour it costs to run a truck (ATRI's figure for 2023).
  //   Starting fuel, 50 miles to a full tank in 50-mile steps. Less than that is not setting off, and a tank
  //   cannot hold more than its range.
  const costLimits = () => ({ min: Math.min(1, DEFAULTS.stopCost), max: Math.max(45, DEFAULTS.stopCost), step: 1 });
  const fuelLimits = (range) => ({ min: Math.min(50, range), max: range, step: 50 });

  // The page re-plans a moment after the last change, so a run of clicks or a spin of the wheel is one request.
  let planTimer = 0;
  function planSoon() {
    clearTimeout(planTimer);
    $('strip').classList.add('busy');
    planTimer = setTimeout(replan, 300);
  }

  // Where one step from a value lands: on the next multiple of the step, and never past either end.
  function stepFrom(value, direction, { min, max, step }) {
    if (direction > 0 ? value >= max : value <= min) return value;
    const next = direction > 0 ? (Math.floor(value / step) + 1) * step : (Math.ceil(value / step) - 1) * step;
    return Math.min(max, Math.max(min, next));
  }

  // A number with an arrow either side. An arrow moves it one step, and so does scrolling over it: up for
  // more, down for less. A value from outside the limits (from a link, say) is shown as it is.
  function counter(id, label) {
    const box = $(id), [less, more] = box.querySelectorAll('button'), out = box.querySelector('output');
    const state = { value: 0, min: 0, max: 0, step: 1 };
    const paint = () => {
      out.textContent = label(state);
      less.disabled = state.value <= state.min;
      more.disabled = state.value >= state.max;
    };
    const move = (direction) => {
      const next = stepFrom(state.value, direction, state);
      if (!current || next === state.value) return;
      state.value = next;
      paint();
      planSoon();
    };
    less.addEventListener('click', () => move(-1));
    more.addEventListener('click', () => move(1));
    // A wheel sends one large movement per notch and a trackpad a stream of small ones, so movement is
    // added up and every 100 units of it is one step. A notch counts as 100 however the browser reports it.
    let travelled = 0, travelledAt = 0;
    box.addEventListener('wheel', (event) => {
      event.preventDefault();  // the page behind stays where it is
      const amount = Math.max(-100, Math.min(100, event.deltaMode === 0 ? event.deltaY : event.deltaY * 40));
      if (event.timeStamp - travelledAt > 300 || amount * travelled < 0) travelled = 0;  // a new gesture
      travelledAt = event.timeStamp;
      travelled += amount;
      if (Math.abs(travelled) < 100) return;
      travelled = 0;
      move(amount < 0 ? 1 : -1);
    }, { passive: false });
    return { get value() { return state.value; }, set(changes) { Object.assign(state, changes); paint(); } };
  }

  const cost = counter('cost', ({ value }) => '$' + Number(value.toFixed(2)));
  const fuel = counter('fuel', ({ value, max }) => Math.round(value) + ' mi' + (value >= max ? ' (full)' : ''));
  cost.set({ ...costLimits(), value: DEFAULTS.stopCost });
  fuel.set({ ...fuelLimits(DEFAULTS.rangeMiles), value: DEFAULTS.rangeMiles });

  // Clicking a tab raises the drawer on it; clicking the open tab again, or the arrow, lowers it.
  $('tabs').addEventListener('click', (event) => {
    if (!current) return;
    const picked = event.target.closest('[data-tab]');
    if (event.target.closest('#collapse')) {
      dockOpen = false;
    } else if (picked) {
      dockOpen = !(dockOpen && picked.dataset.tab === tab);
      tab = picked.dataset.tab;
    } else {
      return;
    }
    showPanel();
    rememberTab();
  });
  $('panel').addEventListener('mouseover', (event) => {
    const row = event.target.closest('tr.pick');
    pickOutPlan(row ? Number(row.dataset.cost) : null);
  });
  $('panel').addEventListener('mouseleave', () => pickOutPlan(null));
  $('panel').addEventListener('click', (event) => {
    const row = event.target.closest('tr.pick');
    if (!row) return;
    cost.set({ value: Number(row.dataset.cost) });
    replan();
  });

  // ---------- server settings drawer ----------
  // Anyone can read the settings. Changing them is for a signed-in account,
  // because they apply to every client; the server enforces that, not this page.

  let server = null;  // the last answer from the settings endpoint
  let signingIn = false;  // whether a visitor has asked for the sign-in fields

  const csrfToken = () => (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || '';

  async function send(method, url, payload) {
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
    } catch (error) {
      return { status: 0, body: { error: { code: 'network_error', message: 'Could not reach the server.' } } };
    }
    let body;
    try { body = await response.json(); } catch (error) { body = { error: { code: 'bad_response', message: `The server answered HTTP ${response.status}.` } }; }
    return { status: response.status, body };
  }

  function shown(setting, value) {
    if (setting.unit === 'USD') return '$' + value;
    if (setting.unit) return `${value} ${setting.unit}`;
    const provider = server.providers.find((candidate) => candidate.name === value);
    return provider ? provider.label : value;
  }

  function field(setting, editable) {
    if (!editable) return `<b>${esc(shown(setting, setting.value))}</b>`;
    if (!setting.unit) {
      return `<select data-setting="${esc(setting.key)}">${server.providers.map((provider) =>
        `<option value="${esc(provider.name)}" ${provider.name === setting.value ? 'selected' : ''}>${esc(provider.label)}</option>`).join('')}</select>`;
    }
    return `<span class="amount">${setting.unit === 'USD' ? '$' : ''}<input type="number" step="any" min="0" data-setting="${esc(setting.key)}" value="${esc(setting.value)}">${setting.unit === 'USD' ? '' : ' ' + esc(setting.unit)}</span>`;
  }

  // The API key belongs to the provider it unlocks, so it is asked for right under
  // the provider choice, and only while a provider that needs one is selected.
  function showKeyBox() {
    const box = $('keyBox');
    if (!box) return;
    const choice = $('settingsForm').querySelector('[data-setting="routing.provider"]');
    const name = choice ? choice.value : server.settings.find((setting) => setting.key === 'routing.provider').value;
    const provider = server.providers.find((candidate) => candidate.name === name);
    if (!provider || !provider.needs_key) {
      box.className = '';
      box.innerHTML = choice ? '' : '<span class="tag">no API key needed</span>';
      return;
    }
    const stored = provider.has_key ? '<span class="tag good">API key stored</span>' : '<span class="tag warn">no API key stored</span>';
    if (!choice || !server.editor.can_set_keys) {
      box.className = '';
      box.innerHTML = stored + (choice ? ' <span class="note">This account may not store API keys.</span>' : '');
      return;
    }
    box.className = 'keybox';
    // "OpenRouteService (API key)" reads oddly after "API key for", so the bracket is dropped here.
    const providerName = provider.label.replace(/\s*\(.*\)$/, '');
    // Only ever link to a real web address, whatever the server sends.
    const keyPage = /^https:\/\//.test(provider.key_page || '') ? provider.key_page : null;
    box.innerHTML = `<label for="providerKey">API key for ${esc(providerName)} ${stored}</label>
      <input type="password" id="providerKey" autocomplete="off" data-key-for="${esc(provider.name)}"
        placeholder="${provider.has_key ? 'Paste a new key to replace the stored one' : 'Paste the API key'}">
      ${keyPage ? `<a class="getkey" href="${esc(keyPage)}" target="_blank" rel="noopener noreferrer">${provider.has_key ? 'Get another key' : 'No key yet? Get a free one'} from ${esc(providerName)} &#8599;</a>` : ''}
      <p class="note">${provider.has_key ? 'Leave this empty to keep the stored key.' : 'Needed before the server can switch to this provider.'}
        It is stored encrypted and never shown again.</p>
      <div class="problem" data-problem="provider_keys.${esc(provider.name)}"></div>`;
  }

  function renderSettings(note) {
    const editor = server.editor;
    const rows = server.settings.map((setting) => {
      const changed = setting.value !== setting.default;
      return `<div class="setting">
        <label class="name"><span>${esc(setting.label)}</span>${field(setting, editor.can_edit)}</label>
        ${setting.key === 'routing.provider' ? '<div id="keyBox"></div>' : ''}
        <p>${esc(setting.description)}</p>
        <p><code>${esc(setting.key)}</code>${changed ? ` &middot; <span class="changed">changed from the default, ${esc(shown(setting, setting.default))}</span>` : ''}
          ${changed && editor.can_edit ? `<button type="button" class="link small" data-restore="${esc(setting.key)}" data-default="${esc(setting.default)}">use the default</button>` : ''}</p>
        <div class="problem" data-problem="${esc(setting.key)}"></div>
      </div>`;
    }).join('');
    let foot;
    if (editor.can_edit) {
      foot = `<div class="actions"><button type="submit" class="primary" id="saveSettings" disabled>Save changes</button><span class="note" id="saveNote">${esc(note || '')}</span></div>
        <p class="note">Saved settings apply to every request from then on. Signed in as <b>${esc(editor.username)}</b>.
        <button type="button" class="link small" id="signOut">Sign out</button></p>`;
    } else if (editor.signed_in) {
      foot = `<p class="note">Signed in as <b>${esc(editor.username)}</b>, which may not change these.
        <button type="button" class="link small" id="signOut">Sign out</button></p>`;
    } else if (!signingIn) {
      foot = `<div class="actions"><button type="button" class="secondary" id="showSignIn">Sign in to edit</button>
        <span class="note">They apply to every request, so changing them needs an admin account.</span></div>`;
    } else {
      foot = `<div class="signin"><h3>Sign in to change these</h3>
        <input type="text" id="loginName" placeholder="Username" autocomplete="username" aria-label="Username">
        <input type="password" id="loginPassword" placeholder="Password" autocomplete="current-password" aria-label="Password">
        <div class="problem" id="loginProblem"></div>
        <button type="submit" class="primary" id="signIn">Sign in</button></div>`;
    }
    $('settingsForm').innerHTML = rows + `<div class="foot">${foot}</div>`;
    showKeyBox();
  }

  async function loadSettings(note) {
    const answer = await send('GET', config.settingsUrl);
    if (answer.status !== 200) {
      $('settingsForm').innerHTML = '<div class="error">Could not read the settings from the server.</div>';
      return;
    }
    server = answer.body;
    // The page was told the defaults when it loaded; they may have changed since.
    for (const setting of server.settings) {
      const name = { 'stops.cost_per_stop': 'stopCost', 'vehicle.range_miles': 'rangeMiles', 'vehicle.mpg': 'mpg', 'routing.provider': 'provider' }[setting.key];
      if (name) DEFAULTS[name] = setting.value;
    }
    renderSettings(note);
  }

  // The settings drawer sits beside the page instead of covering it, so a saved
  // change can be watched taking effect. (On a phone there is no room: it covers.)
  function openSettings() {
    if ($('about').classList.contains('open')) closeAbout();
    $('settings').classList.add('open');
    document.body.classList.add('settings-open');
    $('showSettings').setAttribute('aria-expanded', 'true');
    $('closeSettings').focus();
    $('settingsForm').innerHTML = '<p class="note">Reading the settings...</p>';
    loadSettings();
  }

  function closeSettings() {
    signingIn = false;
    $('settings').classList.remove('open');
    document.body.classList.remove('settings-open');
    $('showSettings').setAttribute('aria-expanded', 'false');
    $('showSettings').focus();
  }

  // What the form holds that differs from what the server has.
  function pendingChanges() {
    const settings = {}, keys = {};
    for (const input of $('settingsForm').querySelectorAll('[data-setting]')) {
      const setting = server.settings.find((candidate) => candidate.key === input.dataset.setting);
      const value = setting.unit ? (input.value.trim() === '' ? '' : Number(input.value)) : input.value;
      if (value !== setting.value) settings[setting.key] = value;
    }
    for (const input of $('settingsForm').querySelectorAll('[data-key-for]')) {
      if (input.value.trim()) keys[input.dataset.keyFor] = input.value.trim();
    }
    const changes = {};
    if (Object.keys(settings).length) changes.settings = settings;
    if (Object.keys(keys).length) changes.provider_keys = keys;
    return changes;
  }

  async function saveSettings() {
    const changes = pendingChanges();
    if (!Object.keys(changes).length) return;
    const wanted = server.providers.find((provider) => provider.name === (changes.settings || {})['routing.provider']);
    if (wanted && wanted.needs_key && !wanted.has_key && !(changes.provider_keys || {})[wanted.name] && $('providerKey')) {
      $('settingsForm').querySelector(`[data-problem="provider_keys.${CSS.escape(wanted.name)}"]`).textContent = 'Paste the API key to switch to this provider.';
      $('providerKey').focus();
      return;
    }
    $('saveSettings').disabled = true;
    $('saveNote').textContent = 'Saving...';
    const answer = await send('PATCH', config.settingsUrl, changes);
    if (answer.status !== 200) {
      // Put each problem under its field; anything else goes next to the button.
      const fields = (answer.body.error && answer.body.error.fields) || {};
      $('settingsForm').querySelectorAll('[data-problem]').forEach((slot) => {
        slot.textContent = [].concat(fields[slot.dataset.problem] || []).join(' ');
      });
      $('saveNote').textContent = Object.keys(fields).length ? 'Not saved: see above.' : ((answer.body.error && answer.body.error.message) || 'Not saved.');
      $('saveSettings').disabled = false;
      if (answer.status === 401 || answer.status === 403) loadSettings();  // the session ended
      return;
    }
    server = answer.body;
    for (const setting of server.settings) {
      const name = { 'stops.cost_per_stop': 'stopCost', 'vehicle.range_miles': 'rangeMiles', 'vehicle.mpg': 'mpg', 'routing.provider': 'provider' }[setting.key];
      if (name) DEFAULTS[name] = setting.value;
    }
    renderSettings('Saved.');
    // Show the effect straight away: the plan on screen is redone under the new settings.
    if (current) {
      const saved = changes.settings || {};
      if ('stops.cost_per_stop' in saved) cost.set({ ...costLimits(), value: DEFAULTS.stopCost });
      if ('vehicle.range_miles' in saved) fuel.set({ ...fuelLimits(DEFAULTS.rangeMiles), value: DEFAULTS.rangeMiles });
      comparisons.clear();
      replan();
    }
  }

  async function signIn() {
    $('signIn').disabled = true;
    const answer = await send('POST', config.sessionUrl, { username: $('loginName').value, password: $('loginPassword').value });
    if (answer.status === 200) { loadSettings(); return; }
    $('loginProblem').textContent = (answer.body.error && answer.body.error.message) || 'Could not sign in.';
    $('signIn').disabled = false;
  }

  $('settingsForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if ($('signIn')) signIn(); else saveSettings();
  });
  $('settingsForm').addEventListener('input', (event) => {
    if (event.target.dataset.setting === 'routing.provider') showKeyBox();
    if ($('saveSettings')) { $('saveSettings').disabled = !Object.keys(pendingChanges()).length; $('saveNote').textContent = ''; }
  });
  $('settingsForm').addEventListener('click', async (event) => {
    if (event.target.dataset.restore) {
      const input = $('settingsForm').querySelector(`[data-setting="${CSS.escape(event.target.dataset.restore)}"]`);
      input.value = event.target.dataset.default;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (event.target.id === 'signOut') { await send('DELETE', config.sessionUrl); signingIn = false; loadSettings(); }
    if (event.target.id === 'showSignIn') { signingIn = true; renderSettings(); $('loginName').focus(); }
  });
  // ---------- about drawer (its content is plain markup from the server) ----------

  function openAbout() {
    if ($('settings').classList.contains('open')) closeSettings();
    $('about').classList.add('open');
    $('shade').classList.add('open');
    $('closeAbout').focus();
  }

  function closeAbout() {
    $('about').classList.remove('open');
    $('shade').classList.remove('open');
    $('showAbout').focus();
  }

  // The Escape key closes whichever side drawer is open. A click outside closes only the
  // About drawer, which is the one that covers the page.
  function closeOpenDrawer() {
    if ($('settings').classList.contains('open')) closeSettings();
    if ($('about').classList.contains('open')) closeAbout();
  }

  $('showSettings').addEventListener('click', () => ($('settings').classList.contains('open') ? closeSettings() : openSettings()));
  $('closeSettings').addEventListener('click', closeSettings);
  $('showAbout').addEventListener('click', openAbout);
  $('closeAbout').addEventListener('click', closeAbout);
  $('shade').addEventListener('click', closeAbout);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeOpenDrawer(); });

  // One stop, highlighted on the map, the chart and the table together.
  let hot = null;
  document.addEventListener('mouseover', (event) => {
    const marked = event.target.closest ? event.target.closest('[data-order]') : null;
    const order = marked ? marked.dataset.order : null;
    if (order === hot) return;
    document.querySelectorAll('.hot').forEach((node) => node.classList.remove('hot'));
    hot = order;
    if (order) document.querySelectorAll(`[data-order="${CSS.escape(order)}"]`).forEach((node) => node.classList.add('hot'));
  });

  // The map and the chart change size when a drawer opens or closes, not only when the
  // window does, so each watches its own box.
  new ResizeObserver(() => map.invalidateSize({ animate: false })).observe($('map'));
  let chartWidth = 0;
  new ResizeObserver(([entry]) => {
    if (current && dockOpen && tab === 'tradeoff') showSelectedRow();  // the middle moves as the drawer slides open
    const width = entry.contentRect.width;
    if (width === chartWidth) return;  // the drawer sliding open changes only the height
    chartWidth = width;
    if (current && dockOpen && tab === 'timeline') drawChart(current.body);
  }).observe($('panel'));

  // ---------- start ----------

  fetch(config.healthUrl).then((response) => response.json()).then((body) => {
    health = body;
    const ready = body.status === 'ok';
    $('health').className = 'pill ' + (ready ? 'ok' : 'bad');
    $('health').textContent = ready ? `Ready · ${Number(body.stations).toLocaleString()} stations` : 'Server not ready';
    if (current && tab === 'server') showPanel();
  }).catch(() => {
    $('health').className = 'pill bad';
    $('health').textContent = 'Server unreachable';
  });

  // A link such as /map/?start=Chicago, IL&finish=Houston, TX (the API's map_url) skips the questions.
  const asked = new URLSearchParams(location.search);
  if (asked.get('start') && asked.get('finish')) {
    $('start').value = asked.get('start');
    $('finish').value = asked.get('finish');
    const number = (name, fallback) => (asked.get(name) !== null && asked.get(name) !== '' && !Number.isNaN(Number(asked.get(name))) ? Number(asked.get(name)) : fallback);
    $('onboarding').hidden = false;
    firstPlan(buildRequest(asked.get('start'), asked.get('finish'), number('stop_cost', undefined), number('initial_range_miles', undefined), asked.get('provider')));
  } else {
    openOnboarding();
  }
})();
