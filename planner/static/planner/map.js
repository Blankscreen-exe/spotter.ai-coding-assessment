// The map page. It is a client of the public API and nothing more: every plan
// on screen came from POST /api/v1/route/, the same call a reviewer makes in
// Postman, and the "API call" tab shows it.
(() => {
  'use strict';

  const config = JSON.parse(document.getElementById('planner-config').textContent);
  const DEFAULTS = config.defaults;  // what the server uses when a request does not say otherwise
  const COMPARE_COSTS = [0, 1, 2, 5, 10, 20];
  const TABS = ['timeline', 'plan', 'tradeoff', 'api', 'server'];
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
  let tab = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'timeline';
  const comparisons = new Map();  // trip and starting fuel -> rows for the "Stops against cost" tab

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

  function buildRequest(start, finish, cost, fuel) {
    const request = { start, finish };
    if (cost !== DEFAULTS.stopCost) request.stop_cost = cost;
    if (fuel < DEFAULTS.rangeMiles) request.initial_range_miles = fuel;
    return request;
  }

  function errorHtml(call) {
    const error = call.body.error || { code: 'error', message: 'Something went wrong.' };
    const fields = error.fields ? ' ' + Object.entries(error.fields).map(([name, problems]) => `${name}: ${[].concat(problems).join(' ')}`).join('; ') : '';
    return `<div class="error"><b>${esc(error.message + fields)}</b><br><code>HTTP ${esc(call.status)} &middot; ${esc(error.code)}</code></div>`;
  }

  // ---------- map ----------

  const map = L.map('map').setView([39.5, -98.35], 4);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    // OpenStreetMap's tile policy requires a Referer and serves "Access blocked"
    // tiles without one. Django's default Referrer-Policy (same-origin) withholds
    // it from other sites, so the tile requests opt back in, sending the origin only.
    referrerPolicy: 'strict-origin-when-cross-origin',
  }).addTo(map);
  const tripLayer = L.layerGroup().addTo(map);

  function pin(lat, lon, html, size) {
    return L.marker([lat, lon], { icon: L.divIcon({ className: '', html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] }) });
  }

  function popup(title, lines) {
    const box = document.createElement('div');
    const head = document.createElement('b');
    head.textContent = title;
    box.append(head);
    for (const line of lines) { const row = document.createElement('div'); row.textContent = line; box.append(row); }
    return box;
  }

  function drawTrip(body) {
    tripLayer.clearLayers();
    const line = L.geoJSON(body.route, { style: { color: '#1d4ed8', weight: 4 } }).addTo(tripLayer);
    pin(body.start.lat, body.start.lon, '<div class="pin end start" style="width:18px;height:18px"></div>', 18)
      .bindPopup(popup('Start', [body.start.name])).addTo(tripLayer);
    pin(body.finish.lat, body.finish.lon, '<div class="pin end finish" style="width:18px;height:18px"></div>', 18)
      .bindPopup(popup('Finish', [body.finish.name])).addTo(tripLayer);
    for (const stop of body.fuel_stops) {
      const order = Number(stop.order);
      pin(stop.lat, stop.lon, `<div class="pin" data-order="${order}" style="width:28px;height:28px">${order}</div>`, 28)
        .bindPopup(popup(`${order}. ${stop.name}`, [
          `${stop.city}, ${stop.state}, mile ${Math.round(stop.mile_marker)}`,
          `${stop.gallons_purchased.toFixed(1)} gal at $${stop.price_per_gallon.toFixed(3)} = ${money(stop.cost)}`,
        ])).addTo(tripLayer);
    }
    routeBounds = line.getBounds();
    map.invalidateSize();
    // A new trip is framed whole. A slider change keeps whatever view the user has chosen.
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
    const left = 44, right = 20, top = 30, bottom = 38;
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
      <text x="${x(0)}" y="${H - 6}" text-anchor="start">${esc(body.start.name)}</text>
      <text x="${x(D)}" y="${H - 6}" text-anchor="end">${esc(body.finish.name)} &middot; mile ${Math.round(D).toLocaleString()}</text>`;
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

    tradeoff: (body) => {
      const loaded = comparisons.get(comparisonKey(current));
      if (!loaded) loadComparison(current);
      if (!loaded || loaded.loading) return '<p class="note">Planning this trip at each cost-per-stop setting...</p>';
      if (loaded.failed) return errorHtml(loaded.failed);
      // The setting on screen may not be one of the standard rows (the slider goes in $1 steps).
      const rows = loaded.rows.filter((row) => row.cost !== body.planning.stop_cost)
        .concat([{ cost: body.planning.stop_cost, summary: body.summary }]).sort((a, b) => a.cost - b.cost);
      const cheapest = Math.min(...rows.map((row) => row.summary.total_fuel_cost));
      return '<p class="note">The same trip at different cost-per-stop settings. Click a row to use it.</p>' +
        '<table><tr><th>Cost per stop</th><th class="num">Stops</th><th class="num">Fuel bill</th><th class="num">Over the cheapest</th></tr>' +
        rows.map((row) => {
          const over = row.summary.total_fuel_cost - cheapest;
          const tag = (row.cost === 0 ? ' (cheapest possible bill)' : '') + (row.cost === DEFAULTS.stopCost ? ' (server default)' : '');
          return `<tr class="pick ${row.cost === body.planning.stop_cost ? 'current' : ''}" data-cost="${Number(row.cost)}">
            <td>$${Number(row.cost)}${tag}</td><td class="num">${Number(row.summary.fuel_stops)}</td>
            <td class="num">${money(row.summary.total_fuel_cost)}</td><td class="num">${over < 0.005 ? '&ndash;' : '+' + money(over)}</td></tr>`;
        }).join('') + '</table>';
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
      block('Request', `curl -X POST ${url} \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify(call.request)}'`);
      block(`Response: HTTP ${call.status}`, JSON.stringify(body, null, 2));
      return box;
    },

    server: (body) => `<dl class="facts">
        <dt>Health</dt><dd>${health ? esc(health.status) : 'unknown'} (GET ${esc(config.healthUrl)})</dd>
        <dt>Stations loaded</dt><dd>${health && health.stations ? Number(health.stations).toLocaleString() : 'unknown'}</dd>
        <dt>This plan</dt><dd>served from ${esc(body.meta.served_from)}, ${plural(Number(body.meta.routing_api_calls), 'routing call')}, ${Number(body.meta.elapsed_ms)} ms on the server</dd>
        <dt>Stations on this route</dt><dd>${Number(body.meta.stations_considered)}</dd>
        <dt>Routing provider</dt><dd>${esc(body.meta.routing_provider)}</dd>
        <dt>Vehicle</dt><dd>${Number(body.vehicle.max_range_miles)} mile range, ${Number(body.vehicle.miles_per_gallon)} miles per gallon</dd>
      </dl>
      <p class="note">The routing provider, range, miles per gallon and the default cost per stop are rows in a settings table.
      Change them under Server settings (top right); the next request picks them up.</p>`,
  };

  function showPanel() {
    document.querySelectorAll('.tab').forEach((button) => button.classList.toggle('on', button.dataset.tab === tab));
    const content = PANELS[tab](current.body);
    if (typeof content === 'string') $('panel').innerHTML = content;
    else $('panel').replaceChildren(content);
    if (tab === 'timeline') drawChart(current.body);
  }

  // One small request per row. The route is already cached, so none of these reaches the routing provider.
  async function loadComparison(call) {
    const key = comparisonKey(call);
    if (comparisons.has(key)) return;
    comparisons.set(key, { loading: true });
    const rows = [];
    for (const cost of COMPARE_COSTS) {
      const answer = await callApi({
        start: call.request.start, finish: call.request.finish, stop_cost: cost,
        initial_range_miles: call.body.vehicle.initial_range_miles, include_geometry: false,
      });
      if (answer.status !== 200) {
        comparisons.set(key, { failed: answer });
        setTimeout(() => comparisons.delete(key), 5000);  // allow a retry, for instance once a rate limit has passed
        break;
      }
      rows.push({ cost, summary: answer.body.summary });
    }
    if (rows.length === COMPARE_COSTS.length) comparisons.set(key, { rows });
    if (tab === 'tradeoff' && current && comparisonKey(current) === key) showPanel();
  }

  // ---------- showing a plan ----------

  function sliderLabels() {
    $('costOut').textContent = '$' + $('cost').value;
    const miles = Number($('fuel').value);
    $('fuelOut').textContent = Math.round(miles) + ' mi' + (miles >= DEFAULTS.rangeMiles ? ' (full)' : '');
  }

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
    return ` Against your last plan: ${signed(stops, plural(Math.abs(stops), 'stop'))}, ${signed(cost, money(Math.abs(cost)))} fuel.`;
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
    $('cost').max = Math.max(Number($('cost').max), Math.ceil(body.planning.stop_cost));
    $('cost').value = body.planning.stop_cost;
    $('fuel').value = body.vehicle.initial_range_miles;
    sliderLabels();
    $('say').textContent = servedSentence(body.meta) + deltaSentence(previous, body.summary);

    drawTrip(body);
    showPanel();
    const query = new URLSearchParams(call.request);
    query.delete('include_geometry');
    history.replaceState(null, '', `${location.pathname}?${query}#${tab}`);
  }

  async function replan() {
    const ticket = ++sequence;
    $('strip').classList.add('busy');
    const call = await callApi(buildRequest(current.request.start, current.request.finish, Number($('cost').value), Number($('fuel').value)));
    if (ticket !== sequence) return;  // a newer change is already on its way
    $('strip').classList.remove('busy');
    lastCall = call;
    if (call.status === 200) { render(call); return; }
    // Keep the plan that is on screen and say why the new one could not be made.
    $('say').innerHTML = errorHtml(call);
    $('cost').value = current.body.planning.stop_cost;
    $('fuel').value = current.body.vehicle.initial_range_miles;
    sliderLabels();
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
    firstPlan(buildRequest(example.start, example.finish, DEFAULTS.stopCost, example.fuel || DEFAULTS.rangeMiles));
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
    firstPlan(buildRequest($('start').value.trim(), finish, DEFAULTS.stopCost, DEFAULTS.rangeMiles));
  });
  $('back').addEventListener('click', () => show('askStart'));
  $('cancel').addEventListener('click', () => { $('onboarding').hidden = true; });
  $('change').addEventListener('click', openOnboarding);

  // ---------- controls ----------

  $('cost').max = Math.max(20, Math.ceil(DEFAULTS.stopCost));
  $('cost').value = DEFAULTS.stopCost;
  $('fuel').step = DEFAULTS.rangeMiles / 20;
  $('fuel').min = DEFAULTS.rangeMiles / 20;
  $('fuel').max = DEFAULTS.rangeMiles;
  $('fuel').value = DEFAULTS.rangeMiles;
  sliderLabels();
  for (const id of ['cost', 'fuel']) {
    $(id).addEventListener('input', sliderLabels);
    $(id).addEventListener('change', replan);
  }

  $('tabs').addEventListener('click', (event) => {
    if (!event.target.dataset.tab || !current) return;
    tab = event.target.dataset.tab;
    showPanel();
    history.replaceState(null, '', `${location.pathname}${location.search}#${tab}`);
  });
  $('panel').addEventListener('click', (event) => {
    const row = event.target.closest('tr.pick');
    if (!row) return;
    $('cost').value = row.dataset.cost;
    sliderLabels();
    replan();
  });

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

  window.addEventListener('resize', () => { if (current && tab === 'timeline') drawChart(current.body); });

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
    firstPlan(buildRequest(asked.get('start'), asked.get('finish'), number('stop_cost', DEFAULTS.stopCost), number('initial_range_miles', DEFAULTS.rangeMiles)));
  } else {
    openOnboarding();
  }
})();
