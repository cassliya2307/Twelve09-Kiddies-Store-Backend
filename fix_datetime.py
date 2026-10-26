with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\tests\test_paystack.py', 'rb') as f:
    content = f.read()

# Fix the datetime import in test 19 (successfailed test)
old = b'reference = "successfailed_ref"\r\n\r\n    db = _db_session()\r\n    try:\r\n        db.add(\r\n            Payment(\r\n                order_id=order["id"],\r\n                amount=Decimal("100.00"),\r\n                payment_method="CARD",\r\n                provider="paystack",\r\n                provider_reference=reference,\r\n                status=PaymentStatus.SUCCESS,\r\n                currency="NGN",\r\n                paid_at=datetime.now(timezone.utc),\r\n            )'

new = b'reference = "successfailed_ref"\r\n\r\n    db = _db_session()\r\n    try:\r\n        from datetime import datetime, timezone\r\n        db.add(\r\n            Payment(\r\n                order_id=order["id"],\r\n                amount=Decimal("100.00"),\r\n                payment_method="CARD",\r\n                provider="paystack",\r\n                provider_reference=reference,\r\n                status=PaymentStatus.SUCCESS,\r\n                currency="NGN",\r\n                paid_at=datetime.now(timezone.utc),\r\n            )'

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\tests\test_paystack.py', 'wb') as f:
        f.write(content)
    print('Fixed datetime import')
else:
    print('Not found')