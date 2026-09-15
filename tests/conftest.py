"""Integration CI must not silently skip required native/browser dependencies."""
import os
import pytest


def pytest_sessionstart(session):
    if os.environ.get("VESTIGRAPH_REQUIRE_INTEGRATION") != "1":
        return
    import bsdiff4
    import klayout.db
    import vestigraph_scan_core
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        browser.close()


_integration_skips = []

def pytest_runtest_logreport(report):
    if (os.environ.get("VESTIGRAPH_REQUIRE_INTEGRATION") == "1" and report.skipped
            and not hasattr(report, "wasxfail") and any(part in report.nodeid for part in
                ("browser", "celldiff", "scan_backend", "scan_equivalence"))):
        _integration_skips.append(report.nodeid)


@pytest.fixture(autouse=True)
def experimental_skill_tests(request, monkeypatch):
    # Skill tests explicitly exercise the opt-in feature; the normal suite keeps production defaults.
    if "skill" in request.node.nodeid:
        monkeypatch.setenv("VESTIGRAPH_EXPERIMENTAL_SKILLS", "1")


def pytest_sessionfinish(session, exitstatus):
    if _integration_skips:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.write_line("Required integration tests skipped: " + ", ".join(_integration_skips))
