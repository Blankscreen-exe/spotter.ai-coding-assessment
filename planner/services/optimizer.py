"""Choose where to buy fuel along a fixed route.

The objective is

    fuel bill + stop_cost x number of stops

stop_cost is what one extra stop is worth to the driver. At zero the plan is
the cheapest possible fuel bill, which in practice means a dozen small top-ups
chasing every slightly cheaper pump. A few dollars per stop removes those for
well under 1% more fuel spend.

Both solvers rest on the same fact about the fixed-path gas station problem:
in an optimal plan, at every stop the vehicle either

    - buys just enough to reach the next stop empty (the next stop is cheaper), or
    - fills the tank (the next stop is dearer).

The starting fuel needs no special case in the greedy: it is a free fill-up at
a virtual station placed before mile 0, far enough back that the vehicle
reaches mile 0 with exactly the starting fuel left. The destination is a
station priced below everything, so the vehicle always arrives empty.
"""

from dataclasses import dataclass
from typing import Any, NamedTuple

from ..exceptions import NoFeasiblePlan

EPSILON = 1e-9


@dataclass(frozen=True)
class Candidate:
    mile: float
    price: float
    ref: Any = None


@dataclass(frozen=True)
class Purchase:
    candidate: Candidate
    gallons: float
    arrival_range_miles: float


def plan_fuel_stops(candidates, trip_miles, range_miles, mpg, start_range_miles, stop_cost=0.0):
    """Return the Purchases, in route order, that minimise the objective."""
    start_range_miles = min(start_range_miles, range_miles)
    stations = sorted(
        (c for c in candidates if 0 <= c.mile <= trip_miles), key=lambda c: (c.mile, c.price)
    )
    if trip_miles <= start_range_miles + EPSILON:
        return []
    _check_reachable(stations, trip_miles, range_miles, start_range_miles)
    if stop_cost > 0:
        return _cheapest_with_stop_cost(stations, trip_miles, range_miles, mpg, start_range_miles, stop_cost)
    return _cheapest_fuel(stations, trip_miles, range_miles, mpg, start_range_miles)


def _check_reachable(stations, trip_miles, range_miles, start_range_miles):
    """Raise NoFeasiblePlan if some stretch of the route has no station in reach."""
    position, reach = 0.0, start_range_miles
    for mile in [c.mile for c in stations] + [trip_miles]:
        if mile > reach + EPSILON:
            target = 'the destination' if mile == trip_miles else 'the next one'
            raise NoFeasiblePlan(
                f'No fuel station on the route between mile {position:.0f} and mile {reach:.0f}, '
                f'the furthest the vehicle can get; {target} is at mile {mile:.0f}.'
            )
        position, reach = mile, mile + range_miles


def _cheapest_fuel(stations, trip_miles, range_miles, mpg, start_range_miles):
    """Greedy, optimal when stops are free.

    At a station, look ahead one tank's worth of road. If a cheaper station is
    in reach, buy only enough to get to the nearest one. Otherwise this is the
    cheapest fuel for the whole stretch ahead, so fill up and move on to the
    cheapest station in reach.
    """
    miles = [start_range_miles - range_miles] + [c.mile for c in stations] + [trip_miles]
    prices = [0.0] + [c.price for c in stations] + [float('-inf')]
    destination = len(miles) - 1

    purchases = []
    here, fuel = 0, 0.0  # fuel is held as miles of range
    while here != destination:
        reach = miles[here] + range_miles + EPSILON
        cheaper = cheapest = None
        for ahead in range(here + 1, destination + 1):
            if miles[ahead] > reach:
                break
            if prices[ahead] < prices[here]:
                cheaper = ahead
                break
            if cheapest is None or prices[ahead] <= prices[cheapest]:
                cheapest = ahead

        if cheaper is not None:
            target = cheaper
            bought = max(0.0, miles[target] - miles[here] - fuel)
        else:
            target = cheapest
            bought = range_miles - fuel

        if 0 < here and bought > EPSILON:
            purchases.append(Purchase(stations[here - 1], bought / mpg, fuel))
        fuel += bought - (miles[target] - miles[here])
        here = target
    return purchases


class _Arrival(NamedTuple):
    """One way of arriving at a station: fuel on board and objective so far."""

    fuel: float  # miles of range
    cost: float
    previous: Any  # (station index, arrival index) of the stop before, or None
    gallons: float  # bought at that previous stop


def _cheapest_with_stop_cost(stations, trip_miles, range_miles, mpg, start_range_miles, stop_cost):
    """Dynamic programme over (station, fuel on arrival), exact for any stop_cost.

    Every station visited here is a stop where fuel is bought. By the
    fill-or-arrive-empty rule the vehicle can only arrive at a stop empty or
    with (range - distance from the previous stop), so there are few states.

    Two observations keep it linear in (stations x stations within one tank):
    - Topping up at a station costs its own price, so arriving with less fuel
      for proportionally less money is never worse. Arrivals are therefore
      reduced to a frontier ordered by fuel on which "value" (cost minus the
      fuel on board priced at this station) strictly falls.
    - Leaving for a dearer stop means filling up, and the best arrival for
      that is always the last one on the frontier. Leaving for a cheaper stop
      means arriving there empty, and the best arrival is the fullest one that
      still needs fuel to get there, which only moves forward as the target
      moves further away.
    """
    count = len(stations)
    miles = [c.mile for c in stations] + [trip_miles]
    prices = [c.price for c in stations] + [float('-inf')]
    arrivals = [[] for _ in range(count + 1)]
    for index in range(count):
        if miles[index] > start_range_miles + EPSILON:
            break
        arrivals[index].append(_Arrival(start_range_miles - miles[index], 0.0, None, 0.0))

    frontiers = [None] * count
    for here in range(count):
        per_mile = prices[here] / mpg
        frontier, values, best = [], [], float('inf')
        for arrival in sorted(arrivals[here], key=lambda a: (a.fuel, a.cost)):
            value = arrival.cost - arrival.fuel * per_mile
            if value < best - EPSILON:
                frontier.append(arrival)
                values.append(value)
                best = value
        frontiers[here] = frontier
        if not frontier:
            continue

        last = len(frontier) - 1
        pick = 0
        for ahead in range(here + 1, count + 1):
            distance = miles[ahead] - miles[here]
            if distance > range_miles + EPSILON:
                break
            if prices[ahead] < prices[here]:
                while pick < last and frontier[pick + 1].fuel < distance - EPSILON:
                    pick += 1
                if frontier[pick].fuel >= distance - EPSILON:
                    continue  # already carrying enough to pass this station by
                arrivals[ahead].append(_Arrival(
                    fuel=0.0,
                    cost=values[pick] + distance * per_mile + stop_cost,
                    previous=(here, pick),
                    gallons=(distance - frontier[pick].fuel) / mpg,
                ))
            elif frontier[last].fuel < range_miles - EPSILON:
                arrivals[ahead].append(_Arrival(
                    fuel=range_miles - distance,
                    cost=values[last] + range_miles * per_mile + stop_cost,
                    previous=(here, last),
                    gallons=(range_miles - frontier[last].fuel) / mpg,
                ))

    if not arrivals[count]:
        raise NoFeasiblePlan('No combination of fuel stops reaches the destination.')
    step = min(arrivals[count], key=lambda a: a.cost)
    purchases = []
    while step.previous is not None:
        station, index = step.previous
        arrived = frontiers[station][index]
        purchases.append(Purchase(stations[station], step.gallons, arrived.fuel))
        step = arrived
    purchases.reverse()
    return purchases
