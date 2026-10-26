import re

with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\tests\test_paystack.py', 'rb') as f:
    content = f.read()

# Fix the import in the pending test
old = b'    try:\r\n        from app.models import Order\r\n\r\n        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()\r\n        assert payment.status == PaymentStatus.SUCCESS\r\n        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()\r\n        assert refreshed_order.status == OrderStatus.CONFIRMED'

new = b'    try:\r\n        from app.models import Order, OrderStatus\r\n\r\n        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()\r\n        assert payment.status == PaymentStatus.SUCCESS\r\n        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()\r\n        assert refreshed_order.status == OrderStatus.CONFIRMED'

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\tests\test_paystack.py', 'wb') as f:
        f.write(content)
    print('Fixed import')
else:
    print('Not found')