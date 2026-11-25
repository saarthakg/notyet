"""pytest plugin, loaded into the repo's own test run as `-p notyet_lines` (and into Python
processes the tests start, by a sitecustomize): records which lines of the changed files execute. Standard library only, and
it watches nothing but those files: sys.monitoring (3.12+) disables every
other code location after its first event, sys.settrace (3.11) never traces
other files' frames.

Config through the environment:
  NOTYET_TRACE_FILES  os.pathsep-separated absolute paths to watch
  NOTYET_TRACE_OUT    each process writes {path: [lines]} to <this>.<pid> at exit
"""
import atexit
import json
import os
import sys
import threading

_targets: dict[str, str] = {}         # filename as seen in code objects → canonical path
for _p in filter(None, os.environ.get("NOTYET_TRACE_FILES", "").split(os.pathsep)):
    _targets[_p] = _p
    _targets[os.path.realpath(_p)] = _p
_hits: dict[str, set[int]] = {p: set() for p in set(_targets.values())}
_known: dict[str, str | None] = {}


def _canonical(filename: str) -> str | None:
    if filename not in _known:
        _known[filename] = _targets.get(filename) or _targets.get(os.path.realpath(filename))
    return _known[filename]


def _start() -> None:
    if not _hits:
        return
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is not None:
        for tool in (4, 5, 3, 2):
            try:
                monitoring.use_tool_id(tool, "notyet")
            except ValueError:
                continue

            def on_line(code, line, _tool=tool):
                path = _canonical(code.co_filename)
                if path is None:
                    return monitoring.DISABLE
                _hits[path].add(line)
                return monitoring.DISABLE     # one hit per line is all we need

            monitoring.register_callback(tool, monitoring.events.LINE, on_line)
            monitoring.set_events(tool, monitoring.events.LINE)
            return

    def local(frame, event, arg):
        path = _canonical(frame.f_code.co_filename)
        if event == "line" and path is not None:
            _hits[path].add(frame.f_lineno)
        return local

    def call(frame, event, arg):
        path = _canonical(frame.f_code.co_filename)
        if path is None:
            return None
        _hits[path].add(frame.f_lineno)
        return local

    global _tracer
    _tracer = call
    sys.settrace(call)
    threading.settrace(call)


def _save() -> None:
    out = os.environ.get("NOTYET_TRACE_OUT")
    if not out or not _hits:
        return
    data: dict = {k: sorted(v) for k, v in _hits.items()}
    if _tracer is not None and sys.gettrace() is not _tracer:
        data["lost"] = True         # a coverage tool or debugger replaced the trace function: lines went unseen
    path = f"{out}.{os.getpid()}"
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass        # the run outlived the check (a daemon): nobody will read it


_tracer = None
_start()
atexit.register(_save)


def pytest_unconfigure(config):
    _save()
