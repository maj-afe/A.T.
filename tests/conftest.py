"""
Shared pytest fixtures and async test runner hook for at-ipc test suite.
"""

import asyncio
import inspect
from pathlib import Path
import shutil
import tempfile
import uuid

import pytest


def pytest_pyfunc_call(pyfuncitem):
    """Run async test functions natively without external pytest-asyncio plugin."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        testargs = {
            arg: pyfuncitem.funcargs[arg]
            for arg in pyfuncitem._fixtureinfo.argnames
        }
        asyncio.run(pyfuncitem.obj(**testargs))
        return True
    return None


@pytest.fixture
def temp_socket_dir():
    """Create a temporary directory for Unix domain sockets and clean it up after test."""
    temp_dir = Path(tempfile.mkdtemp(prefix=f"at_ipc_test_{uuid.uuid4().hex[:8]}_"))
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
