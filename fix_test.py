with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\tests\test_paystack.py', 'rb') as f:
    content = f.read()

# Fix the pending test
old = b'pending_ref"\r\n\r\n    db = _db_session()\r\n    try:\r\n        db.add(\r\n            Payment(\r\n                order_id=order["id"],\r\n                amount=Decimal("100.00"),\r\n                payment_method="CARD",\r\n                provider="paystack",\r\n                provider_reference=reference,\r\n                status=PaymentStatus.PENDING,\r\n                currency="NGN",\r\n            )\r\n        db.commit()\r\n    finally:\r\n        db.close()\r\n\r\n    event ='

new = b'pending_ref"\r\n\r\n    db = _db_session()\r\n    try:\r\n        db.add(\r\n            Payment(\r\n                order_id=order["id"],\r\n                amount=Decimal("100.00"),\r\n                payment_method="CARD",\r\n                provider="paystack",\r\n                provider_reference=reference,\r\n                status=PaymentStatus.PENDING,\r\n                currency="NGN",\r\n            )\r\n        )\r\n        db.commit()\r\n    finally:\r\n        db.close()\r\n\r\n    event ='

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\tests\test_paystack.py', 'wb') as f:
        f.write(content)
    print('Fixed pending test')
else:
    print('Old string not found')