with open('tests/test_receipt_workflow.py', 'r') as f:
    content = f.read()

# Fix the indentation for test_upload_non_pending_payment_rejected
content = content.replace(
    'def test_upload_non_pending_payment_rejected(self, test_client, pg_db):',
    '    def test_upload_non_pending_payment_rejected(self, test_client, pg_db):'
)

# Fix the upload_other_customer_payment_rejected (ensure it has 4 spaces)
content = content.replace(
    'def test_upload_other_customer_payment_rejected(self, test_client, pg_db):',
    '    def test_upload_other_customer_payment_rejected(self, test_client, pg_db):'
)

with open('tests/test_receipt_workflow.py', 'w') as f:
    f.write(content)

print('Fixed indentation issues')