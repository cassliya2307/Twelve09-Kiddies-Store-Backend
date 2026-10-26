import pytest
import sys
import traceback

# Run the test and capture full traceback
try:
    result = pytest.main(['-xvs', 'tests/test_paystack.py::test_webhook_recovers_previous_reference', '--tb=long', '--no-header'])
except SystemExit as e:
    pass