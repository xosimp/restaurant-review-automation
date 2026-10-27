"""The browser tests (Playwright): their marker, registered here too so this
directory runs on its own before or without the suite-wide registration."""


def pytest_configure(config):
    config.addinivalue_line("markers", "browser: drives a real browser (Playwright) against a keyless app; "
                            "skipped without playwright, run in its own CI job")
