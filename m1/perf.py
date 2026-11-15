"""Server-side timing only; does not measure browser paint or network latency."""

from contextlib import contextmanager
from time import perf_counter


@contextmanager
def measure(state, stage):
    start = perf_counter()
    try:
        yield
    finally:
        timings = dict(state.get("perf_timings", {}))
        timings[stage] = perf_counter() - start
        state["perf_timings"] = timings
