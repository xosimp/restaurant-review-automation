"""
schedule_cpsat_runtime.py — how a CP-SAT solve runs, for every schedule
model (Stage 1's shapes, Stage 2's assignment, the repair re-solve, the
fixed-week evaluations).

  * the budget scaled to the model's size (work_budget: deterministic
    seconds per thousand variables, capped), and the lean portfolio for a
    large model (lean);
  * a large solve in its own solver process (run: exec'd, never forked from
    this multi-threaded one; the model and its parameters go out as protobuf
    text, the answer comes back the same way and is injected into the
    caller's CpSolver), one spare process kept started, credentials kept out
    of its environment, a memory bound, and a dead or hung process killed
    and the same solve run here on the same budget;
  * whether a solve was cut by the wall clock (_wall_capped).

Split out of schedule_cpsat (ARCH-3) so the shapes (Stage 1) and the
assignment (Stage 2) both sit above it: schedule_shapes imported the solver
module that imports it back. schedule_cpsat re-exports every name. Layer S4
of the schedule modules (ARCHITECTURE_MANIFEST.md §3a); it imports no
schedule module. Kill switch: SCHEDULE_CPSAT_ISOLATE=0 (every solve
in-process).
"""
import os
import time as _time

try:
    from ortools.sat.python import cp_model as _cp
except Exception:          # OR-Tools missing: schedule_cpsat.available() is False and no solve starts
    _cp = None


def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


# ── what one solve may use: work, memory, the process (blind re-audit 10/11/26
# STRESS-11, STRESS-12, SCALE-16, SCALE-18, SCALE-19) ─────────────────────────
#
# Work: a deterministic budget scaled to the model — DET_PER_KVAR
# deterministic seconds per thousand variables, never below the caller's
# stated budget (so a small week is solved exactly as before) and never
# above DET_CAP. A fixed 7.5 s Stage 1 found NO week at 250 people (33k
# variables: the draft fell back to the backtracking solver) and the repair
# re-solve's stability phase got 3 s, less than its own presolve, so it
# never ran. A solve still stops the moment it is proved optimal, and a
# phase whose warm start already sits at the phase's least possible cost is
# not solved at all (_at_bound) — that is where a small week's CPU went.
#
# Memory: deterministic search on 4 workers loads NINE full subsolvers, each
# with its own copy of the model and its LP: 1.6-2.0 GB at 250 people. From
# LEAN_MIN_VARS variables up the portfolio keeps default_lp, max_lp and
# no_lp (and the LNS workers): measured at 250 people, Stage 1 proved the
# same optimum in 3.9 s and 0.6 GB (was 6.4 s, 1.6 GB), the stability phase
# proved 469 in 3.8 s and 0.6 GB where the full portfolio reached 530 in
# 5.6 s and 1.1 GB. Every large solve runs in a solver process of its own
# (`run`), so what CP-SAT allocated goes back to the operating system the
# moment it returns — a 250-person generation used to leave the process at
# 1.4 GB for good — and CP-SAT's own max_memory_in_mb bounds it at
# SOLVE_MEMORY_MB. A solve the memory bound ended is recorded
# (`memory_capped`), like one the wall clock ended.
#
# The solver process is EXECED, never forked from this one: a generation
# runs beside another generation's CP-SAT thread pools, the lease and
# heartbeat threads and the database pool, and a fork while any of them
# held a C-level lock (absl's, malloc's, sqlite's) would hand the child that
# lock held for ever. A fresh interpreter that imports only OR-Tools
# (_SOLVER_CODE, started by subprocess — fork-exec, safe from any thread)
# reads the model and the parameters as protobuf text, solves, and writes
# the response back as text: the same proto, the same parameters, the same
# deterministic answer. One spare is kept started (_take_process) so a
# solve does not wait on an interpreter's start-up. A solver process that
# dies or outlives its wall clock plus ISOLATE_GRACE_SECONDS is killed and
# the solve is run in this process on the same budget — recorded
# (`sp_info["fallback"]`), never a failed generation.
DET_PER_KVAR = _env_float("SCHEDULE_CPSAT_DET_PER_KVAR", 0.6)


DET_CAP = _env_float("SCHEDULE_CPSAT_DET_CAP", 120.0)


LEAN_MIN_VARS = int(_env_float("SCHEDULE_CPSAT_LEAN_MIN_VARS", 15000))


LEAN_IGNORE = ("core", "max_lp_sym", "pseudo_costs", "quick_restart", "quick_restart_no_lp", "reduced_costs")


ISOLATE_MIN_VARS = int(_env_float("SCHEDULE_CPSAT_ISOLATE_MIN_VARS", 5000))


SOLVE_MEMORY_MB = _env_float("SCHEDULE_CPSAT_MEMORY_MB", 2048.0)


ISOLATE_GRACE_SECONDS = 30.0


def work_budget(stated, n_vars) -> float:
    """The deterministic budget for a model of `n_vars` variables: the
    stated budget, raised to DET_PER_KVAR per thousand variables, at most
    DET_CAP (never below what was stated)."""
    stated = float(stated)
    want = DET_PER_KVAR * float(n_vars or 0) / 1000.0
    return round(max(stated, min(want, DET_CAP)), 2)


def lean(s, n_vars) -> None:
    """The memory-bounded portfolio on a large model (LEAN_MIN_VARS)."""
    if not n_vars or n_vars < LEAN_MIN_VARS:
        return
    for name in LEAN_IGNORE:
        s.parameters.ignore_subsolvers.append(name)


def isolation() -> str:
    """"process" (the default): a large solve runs in a solver process of its
    own; "off" (SCHEDULE_CPSAT_ISOLATE=0 — the kill switch): in this one."""
    v = str(os.getenv("SCHEDULE_CPSAT_ISOLATE", "process") or "process").strip().lower()
    if v in ("0", "off", "false", "no", "inline"):
        return "off"
    return "process"


# The solver process: a bare interpreter importing OR-Tools only. Reads
# "<params bytes>\n<model bytes>\n" then the two protobuf texts; writes
# "<peak MB>\n" then the CpSolverResponse as text. Empty input: exits.
_SOLVER_CODE = r"""
import os, sys, resource
from ortools.sat.python import cp_model as cp
inp = sys.stdin.buffer
head = inp.readline()
if not head.strip():
    sys.exit(0)
np, nm = (int(x) for x in head.split())
params = inp.read(np).decode()
model = inp.read(nm).decode()
m = cp.CpModel()
m.proto.parse_text_format(model)
del model
s = cp.CpSolver()
s.parameters.parse_text_format(params)
s.solve(m)
out = str(s.response_proto).encode()
r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
peak = r / 1048576.0 if sys.platform == "darwin" else r / 1024.0
sys.stdout.buffer.write(("%.1f\n" % peak).encode() + out)
sys.stdout.buffer.flush()
"""


_SPARE = []


_SPARE_LOCK = __import__("threading").Lock()


_SECRET_ENV = ("_KEY", "_TOKEN", "_SECRET", "PASSWORD", "DATABASE_URL", "_DSN")


def _solver_env() -> dict:
    """The solver process's environment: this one's, without a credential."""
    return {k: v for k, v in os.environ.items() if not any(t in k.upper() for t in _SECRET_ENV)}


def _start_process():
    import subprocess
    import sys as _sys_p
    return subprocess.Popen([_sys_p.executable, "-c", _SOLVER_CODE], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_solver_env(),
                            close_fds=True)


def _take_process():
    """A started solver process — the spare when one is waiting — and a new
    spare started behind it."""
    with _SPARE_LOCK:
        proc = None
        while _SPARE and proc is None:
            cand = _SPARE.pop()
            if cand.poll() is None:
                proc = cand
        if proc is None:
            proc = _start_process()
        try:
            _SPARE.append(_start_process())
        except Exception:
            pass
        return proc


def _rss_mb() -> float:
    """This process's resident memory now (Linux), else its peak, MB."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576.0
    except Exception:
        return _peak_mb()


def _peak_mb() -> float:
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / 1048576.0 if os.uname().sysname == "Darwin" else r / 1024.0
    except Exception:
        return 0.0


def _trim() -> None:
    """Hand freed heap back to the OS after an in-process solve (glibc)."""
    try:
        import ctypes
        import ctypes.util
        name = ctypes.util.find_library("c")
        if name and "libc.so" in name:
            ctypes.CDLL(name).malloc_trim(0)
    except Exception:
        pass


_RESPONSE_FIELDS = ("objective_value", "best_objective_bound", "deterministic_time", "wall_time", "user_time",
                    "num_conflicts", "num_branches", "num_booleans")


def _answer(status, solution, fields=None, params=None):
    """A CpSolver holding a response built here — a forked child's answer,
    or a phase proved at its bound with no search — read exactly like one
    that solved in this process (value, boolean_value, objective_value,
    status_name, response_proto)."""
    import ortools.sat.python.cp_model_helper as _cmh
    resp = _cmh.CpSolverResponse()
    resp.status = _cmh.CpSolverStatus(int(status))
    if solution:
        resp.solution.extend([int(v) for v in solution])
    for k, v in (fields or {}).items():
        if v is not None:
            setattr(resp, k, v)
    s = _cp.CpSolver()
    if params is not None:
        s.parameters.copy_from(params)
    s._CpSolver__response = resp
    return s


def run(s, model, isolate=True):
    """s.solve(model), with what it uses bounded (see the block above): a
    model of ISOLATE_MIN_VARS variables or more (and `isolate`) in a solver
    process of its own; CP-SAT's memory bound at SOLVE_MEMORY_MB over what
    the solving process holds. The answer is the same either way — the same
    model and parameters, a deterministic search. `s.sp_info` says how it
    ran: {"isolated", "peak_mb", "memory_capped", "fallback", "note"}."""
    n_vars = len(model.proto.variables)
    if isolate and isolation() == "process" and n_vars >= ISOLATE_MIN_VARS:
        got = _run_in_process(s, model)
        if got is not None:
            return got
    note = (getattr(s, "sp_info", None) or {}).get("note")
    limit = int(_rss_mb() + SOLVE_MEMORY_MB)
    s.parameters.max_memory_in_mb = limit
    st = s.solve(model)
    peak = _peak_mb()
    s.sp_info = {"isolated": False, "peak_mb": round(peak, 1), "limit_mb": limit,
                 "memory_capped": _memory_capped(s, st, peak, limit),
                 "fallback": bool(note), "note": note}
    _trim()
    return st


def _memory_capped(s, st, peak, limit) -> bool:
    """Whether CP-SAT's memory bound, not its work budget or the clock,
    ended a solve that did not prove its answer."""
    if st == _cp.OPTIMAL or not limit:
        return False
    try:
        det = float(s.response_proto.deterministic_time)
        wall = float(s.response_proto.wall_time)
    except Exception:
        return False
    p = s.parameters
    return (peak >= 0.9 * limit and det < 0.95 * float(p.max_deterministic_time or 1e18)
            and wall < 0.95 * float(p.max_time_in_seconds or 1e18))


def _run_in_process(s, model):
    """The solve in a solver process (_SOLVER_CODE): the model and the
    parameters out as protobuf text, the response back, injected into `s`;
    the status returned. None — with `s.sp_info["note"]` saying why — when
    the process could not start, died, answered nothing readable, or
    outlived its wall clock plus ISOLATE_GRACE_SECONDS (it is killed): the
    caller then solves in this process on the same budget."""
    import subprocess
    import ortools.sat.python.cp_model_helper as _cmh
    s.parameters.max_memory_in_mb = int(SOLVE_MEMORY_MB + 150)   # the process starts at ~150 MB
    limit = int(s.parameters.max_memory_in_mb)
    try:
        params = str(s.parameters).encode()
        body = str(model.proto).encode()
    except Exception as e:
        s.sp_info = {"isolated": False, "note": f"model not serialised: {e!r}"[:200]}
        return None
    payload = f"{len(params)} {len(body)}\n".encode() + params + body
    del body
    timeout = float(s.parameters.max_time_in_seconds or 60.0) + ISOLATE_GRACE_SECONDS
    try:
        proc = _take_process()
    except Exception as e:
        s.sp_info = {"isolated": False, "note": f"solver process did not start: {e!r}"[:200]}
        return None
    try:
        out, _err = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        s.sp_info = {"isolated": False, "note": "the solver process outlived its wall clock and was killed; "
                                                "solved here on the same budget"}
        return None
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        s.sp_info = {"isolated": False, "note": f"the solver process failed ({e!r}); solved here"[:200]}
        return None
    head, _sep, text = (out or b"").partition(b"\n")
    if proc.returncode != 0 or not text:
        why = "was killed (out of memory?)" if (proc.returncode or 0) < 0 else f"exited {proc.returncode}"
        s.sp_info = {"isolated": False, "note": f"the solver process {why} without an answer; solved here"}
        return None
    try:
        resp = _cmh.CpSolverResponse()
        resp.parse_text_format(text.decode())
        peak = float(head or 0.0)
    except Exception as e:
        s.sp_info = {"isolated": False, "note": f"the solver's answer was unreadable ({e!r}); solved here"[:200]}
        return None
    s._CpSolver__response = resp
    st = resp.status
    s.sp_info = {"isolated": True, "peak_mb": round(peak, 1), "limit_mb": limit,
                 "memory_capped": _memory_capped(s, st, peak, limit), "fallback": False}
    return st


def _wall_capped(solver, status) -> bool:
    """Whether the wall clock (the emergency cap), not the deterministic work
    budget, ended a solve that did not prove its answer optimal."""
    if status == _cp.OPTIMAL or not solver.parameters.max_deterministic_time:
        return False
    try:
        det = float(solver.response_proto.deterministic_time)
    except Exception:
        return False
    return det < 0.95 * float(solver.parameters.max_deterministic_time) and \
        float(solver.wall_time) >= 0.95 * float(solver.parameters.max_time_in_seconds)
