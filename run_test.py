import pytest
import sys

result = pytest.main(['-xvs', 'tests/test_paystack.py::test_webhook_recovers_previous_reference', '--tb=long'])
sys.exit(0)