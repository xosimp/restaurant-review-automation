"""The iPhone's Home reads again on appear only when its last read is stale
(scale audit 10/19/26 FE-07): every appear in the tab bar re-ran the summary
and, keyed on it, the follow-through, the brief, last night, the activity
strip and the Bar."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Home")


def _read(name):
    return open(os.path.join(HOME, name), encoding="utf-8").read()


def test_an_appear_loads_only_a_stale_home():
    view = _read("HomeView.swift")
    assert ".task { await viewModel.loadIfStale() }" in view
    assert ".task { await viewModel.load() }" not in view


def test_the_explicit_reads_still_load():
    view = _read("HomeView.swift")
    # A pull, a location switch and the return from the background.
    assert ".cavnarEmberRefreshable { await viewModel.load(fresh: true) }" in view
    assert re.search(r"locationSwitches\) \{ _, _ in\s+Task \{ await viewModel\.load\(\) \}", view)
    assert ".refreshOnForeground(lastLoaded: viewModel.lastLoadedAt) { await viewModel.load() }" in view


def test_the_guard_is_a_freshness_window_on_the_last_load():
    vm = _read("HomeViewModel.swift")
    body = vm.split("func loadIfStale(now: Date = Date()) async {", 1)[1].split("\n    }\n", 1)[0]
    assert "adoptCurrentSession()" in body          # a new session or location always loads
    assert "lastLoadedAt" in body and "appearFreshSeconds" in body and "await load()" in body
    m = re.search(r"static let appearFreshSeconds: TimeInterval = (\d+)", vm)
    assert m and 60 <= int(m.group(1)) <= 300
