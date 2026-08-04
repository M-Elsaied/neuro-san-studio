"""
Pytest configuration scoped to the servicenow test subtree.
"""


def pytest_configure(config):
    """
    :param config: The pytest config object.
    """
    # The repo's pyproject does not declare this marker; registering it here keeps
    # the suite warning-free without touching shared configuration.
    config.addinivalue_line(
        "markers", "asyncio: async test method run natively by IsolatedAsyncioTestCase")
