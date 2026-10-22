"""Exact per-farmer leximin by water-filling on a flow network (used when every lot has the same increment).

Why it is exact. With one common increment, the vectors of farmer totals u (in increments) that some
integer plan can reach form an integral polymatroid: lots feed mandi-date slots with integer capacities,
and grouping lots by farmer keeps that structure. Leximin of r_f = u_f / P_f over a finite set equals
maximising sum_f -exp(-M r_f) for every large enough M (the smallest ratio dominates, then the next).
That objective is separable and concave in u_f, and over an integral polymatroid the greedy procedure that
repeatedly adds one unit to the coordinate with the largest marginal gain, while staying feasible, is
optimal (Federgruen and Groenevelt, Oper. Res. 34(6), 1986; for flows, Megiddo, Math. Program. 7, 1974).
For large M that marginal-gain order is: smallest current ratio first, then smaller P_f, then farmer ID
(exact ties have equal gains for every M). Feasibility of "one more unit for farmer f" is an augmenting
path f -> lot -> slot -> sink in the residual network that may reroute, but never reduce, other farmers.
The greedy ends at a base of the polymatroid, so it also maximises total allocation (all bases have the
same size). The oracle tests in core/tests/test_allocation.py check this method against enumeration.
"""

from __future__ import annotations

import heapq
from collections import deque
from datetime import date
from fractions import Fraction

from .model import AllocationInput, MarketLoad

_INF = 1 << 62


class _Graph:
    def __init__(self) -> None:
        self.adj: list[list[int]] = []
        self.to: list[int] = []
        self.cap: list[int] = []

    def node(self) -> int:
        self.adj.append([])
        return len(self.adj) - 1

    def edge(self, a: int, b: int, cap: int) -> int:
        self.adj[a].append(len(self.to))
        self.to.append(b)
        self.cap.append(cap)
        self.adj[b].append(len(self.to))
        self.to.append(a)
        self.cap.append(0)
        return len(self.to) - 2


def uniform_increment(inp: AllocationInput) -> int | None:
    incs = {lot.increment.units for lot in inp.lots}
    return incs.pop() if len(incs) == 1 else None


def leximin_flow(inp: AllocationInput) -> tuple[dict[tuple[str, str, date], int], dict[str, int]]:
    """Return (increments per choice key, increments per farmer) of a leximin-optimal plan."""
    inc = uniform_increment(inp)
    if inc is None:
        raise ValueError("leximin_flow needs one common increment")
    g = _Graph()
    sink = g.node()
    fnode = {f: g.node() for f in inp.farmers}
    lnode = {}
    for lot in inp.lots:
        lnode[lot.id] = g.node()
        g.edge(fnode[lot.farmer_id], lnode[lot.id], lot.max_increments)
    snode: dict[tuple[str, date], int] = {}
    for c in inp.choices_canonical:
        s = (c.mandi_id, c.date)
        if s not in snode:
            snode[s] = g.node()
            cap = inp.capacities[s].units // inc if inp.market_load is MarketLoad.STRICT else _INF
            g.edge(snode[s], sink, cap)
    choice_edge = {c.key: g.edge(lnode[c.lot_id], snode[(c.mandi_id, c.date)], _INF) for c in inp.choices_canonical}

    def augment(src: int, want: int) -> int:
        """Push up to `want` units from farmer node `src` to the sink along one shortest residual path."""
        prev = {src: -1}
        q = deque([src])
        while q and sink not in prev:
            v = q.popleft()
            for e in g.adj[v]:
                w = g.to[e]
                if g.cap[e] > 0 and w not in prev:
                    # Passing through another farmer node reroutes its lots; its total is unchanged.
                    prev[w] = e
                    q.append(w)
        if sink not in prev:
            return 0
        path, v = [], sink
        while v != src:
            e = prev[v]
            path.append(e)
            v = g.to[e ^ 1]
        push = min([want] + [g.cap[e] for e in path])
        for e in path:
            g.cap[e] -= push
            g.cap[e ^ 1] += push
        return push

    P = {f: inp.participating_g(f) for f in inp.fairness_farmers}
    units = dict.fromkeys(P, 0)

    def key(f: str) -> tuple[Fraction, int, str]:
        return (Fraction(units[f] * inc, P[f]), P[f], f)

    heap = [key(f) for f in P]
    heapq.heapify(heap)
    while heap:
        _, _, f = heapq.heappop(heap)
        if heap:  # how many units f can take before it stops being first in the gain order
            r_next, p_next, f_next = heap[0]
            t = r_next * P[f] / inc  # f's ratio equals the next ratio at u_f = t
            floor_t, ceil_t = t.numerator // t.denominator, -(-t.numerator // t.denominator)
            # at equality the smaller (P, ID) goes first: if f is not that one, it loses first place at ceil(t)
            need = (ceil_t if (P[f], f) > (p_next, f_next) else floor_t + 1) - units[f]
            need = max(1, need)
        else:
            need = _INF
        got = augment(fnode[f], need)
        if got == 0:
            continue  # f cannot grow any more (polymatroid: it never can again)
        units[f] += got
        heapq.heappush(heap, key(f))
    n = {k: g.cap[e ^ 1] for k, e in choice_edge.items() if g.cap[e ^ 1] > 0}
    return n, units
