import itertools
import random

from django.test import SimpleTestCase

from planner.exceptions import NoFeasiblePlan
from planner.services.optimizer import Candidate, plan_fuel_stops

RANGE, MPG = 500.0, 10.0


def fuel_bill(purchases):
    return sum(p.gallons * p.candidate.price for p in purchases)


def drive(purchases, trip_miles, start_range):
    """Replay a plan, asserting the tank never runs dry or overflows. Returns fuel left."""
    position, fuel = 0.0, start_range
    for purchase in purchases:
        fuel -= purchase.candidate.mile - position
        assert fuel > -1e-6, f'ran dry before mile {purchase.candidate.mile}'
        assert abs(fuel - purchase.arrival_range_miles) < 1e-6
        assert purchase.gallons > 0
        fuel += purchase.gallons * MPG
        assert fuel < RANGE + 1e-6, f'overfilled at mile {purchase.candidate.mile}'
        position = purchase.candidate.mile
    fuel -= trip_miles - position
    assert fuel > -1e-6, 'ran dry before the destination'
    return fuel


def lowest_possible_bill(stations, trip_miles, start_range):
    """Independent oracle for the fuel bill of the best plan using these stations.

    Each mile of road is best served by the cheapest station in the tank's
    worth of road behind it; the starting fuel is a free station that far back.
    Returns None when some mile has no station behind it in range.
    """
    sources = [(start_range - RANGE, 0.0)] + [(c.mile, c.price) for c in stations]
    edges = sorted({0.0, trip_miles} | {
        x for mile, _ in sources for x in (mile, mile + RANGE) if 0 < x < trip_miles
    })
    total = 0.0
    for left, right in zip(edges, edges[1:], strict=False):
        middle = (left + right) / 2
        in_reach = [price for mile, price in sources if mile <= middle <= mile + RANGE]
        if not in_reach:
            return None
        total += (right - left) / MPG * min(in_reach)
    return total


def best_objective_by_brute_force(stations, trip_miles, start_range, stop_cost):
    best = None
    for size in range(len(stations) + 1):
        for subset in itertools.combinations(stations, size):
            bill = lowest_possible_bill(subset, trip_miles, start_range)
            if bill is not None and (best is None or bill + stop_cost * size < best):
                best = bill + stop_cost * size
    return best


def random_stations(rng, count, trip_miles):
    return [
        Candidate(mile=round(rng.uniform(0, trip_miles), 1), price=round(rng.uniform(2.5, 4.5), 3))
        for _ in range(count)
    ]


class GreedyTests(SimpleTestCase):
    def plan(self, stations, trip_miles, start_range=RANGE):
        return plan_fuel_stops(stations, trip_miles, RANGE, MPG, start_range)

    def test_trip_within_starting_fuel_needs_no_stop(self):
        self.assertEqual(self.plan([Candidate(100, 3.0)], 400), [])

    def test_buys_only_the_shortfall_at_the_cheapest_station(self):
        stations = [Candidate(200, 4.0), Candidate(400, 3.0)]
        (purchase,) = self.plan(stations, 700)
        self.assertEqual(purchase.candidate.mile, 400)
        self.assertAlmostEqual(purchase.gallons, 20.0)
        self.assertAlmostEqual(purchase.arrival_range_miles, 100.0)

    def test_buys_just_enough_to_reach_a_cheaper_station(self):
        stations = [Candidate(400, 4.0), Candidate(600, 3.0)]
        first, second = self.plan(stations, 1000)
        self.assertAlmostEqual(first.gallons, 10.0)   # 100 miles short of mile 600
        self.assertAlmostEqual(second.gallons, 40.0)  # arrives empty, needs 400 miles

    def test_fills_up_when_nothing_cheaper_is_in_reach(self):
        stations = [Candidate(400, 3.0), Candidate(800, 4.0)]
        first, second = self.plan(stations, 1100)
        self.assertAlmostEqual(first.gallons, 40.0)   # tank holds 50, arrives with 10
        self.assertAlmostEqual(second.gallons, 20.0)

    def test_partial_starting_fuel(self):
        (purchase,) = self.plan([Candidate(50, 3.0)], 300, start_range=100)
        self.assertAlmostEqual(purchase.arrival_range_miles, 50.0)
        self.assertAlmostEqual(purchase.gallons, 20.0)

    def test_gap_longer_than_the_range_is_reported(self):
        with self.assertRaisesMessage(NoFeasiblePlan, 'between mile 100 and mile 600'):
            self.plan([Candidate(100, 3.0), Candidate(700, 3.0)], 900)

    def test_no_station_within_starting_fuel_is_reported(self):
        with self.assertRaisesMessage(NoFeasiblePlan, 'between mile 0 and mile 50'):
            self.plan([Candidate(100, 3.0)], 300, start_range=50)

    def test_matches_the_oracle_on_random_routes(self):
        rng = random.Random(7)
        for _ in range(300):
            trip = rng.uniform(200, 3000)
            start = rng.choice([RANGE, RANGE, rng.uniform(0, RANGE)])
            stations = random_stations(rng, rng.randint(0, 40), trip)
            expected = lowest_possible_bill(stations, trip, start)
            if expected is None:
                with self.assertRaises(NoFeasiblePlan):
                    self.plan(stations, trip, start)
                continue
            purchases = self.plan(stations, trip, start)
            self.assertAlmostEqual(fuel_bill(purchases), expected, places=6)
            # No fuel is ever bought that the trip does not burn.
            left_over = drive(purchases, trip, min(start, RANGE))
            self.assertAlmostEqual(left_over, 0.0 if purchases else start - trip, places=6)


class StopCostTests(SimpleTestCase):
    def test_skips_a_small_saving_that_is_not_worth_a_stop(self):
        # Topping up 10 gallons at mile 400 saves 10 x $0.10 = $1 over buying it all at mile 450.
        stations = [Candidate(400, 2.9), Candidate(450, 3.0)]
        free = plan_fuel_stops(stations, 950, RANGE, MPG, RANGE, stop_cost=0)
        costly = plan_fuel_stops(stations, 950, RANGE, MPG, RANGE, stop_cost=5)
        self.assertEqual(len(free), 2)
        self.assertEqual([p.candidate.mile for p in costly], [450])

    def test_still_makes_the_stop_when_the_saving_is_larger(self):
        stations = [Candidate(400, 2.0), Candidate(450, 3.0)]
        costly = plan_fuel_stops(stations, 950, RANGE, MPG, RANGE, stop_cost=5)
        self.assertEqual([p.candidate.mile for p in costly], [400, 450])

    def test_matches_brute_force_on_random_routes(self):
        rng = random.Random(11)
        for _ in range(400):
            trip = rng.uniform(300, 2200)
            start = rng.choice([RANGE, RANGE, rng.uniform(0, RANGE)])
            stop_cost = rng.choice([0.5, 2, 5, 20, 100])
            stations = random_stations(rng, rng.randint(0, 9), trip)
            expected = best_objective_by_brute_force(stations, trip, start, stop_cost)
            if expected is None:
                with self.assertRaises(NoFeasiblePlan):
                    plan_fuel_stops(stations, trip, RANGE, MPG, start, stop_cost)
                continue
            purchases = plan_fuel_stops(stations, trip, RANGE, MPG, start, stop_cost)
            drive(purchases, trip, min(start, RANGE))
            self.assertAlmostEqual(fuel_bill(purchases) + stop_cost * len(purchases), expected, places=6)

    def test_negligible_stop_cost_agrees_with_the_greedy(self):
        rng = random.Random(23)
        for _ in range(200):
            trip = rng.uniform(600, 3300)
            stations = random_stations(rng, rng.randint(20, 120), trip)
            try:
                greedy = plan_fuel_stops(stations, trip, RANGE, MPG, RANGE, stop_cost=0)
            except NoFeasiblePlan:
                continue
            dynamic = plan_fuel_stops(stations, trip, RANGE, MPG, RANGE, stop_cost=1e-7)
            self.assertAlmostEqual(fuel_bill(dynamic), fuel_bill(greedy), places=4)
