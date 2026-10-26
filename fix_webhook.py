with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'rb') as f:
    content = f.read()

# Fix the webhook fallback lookup to work with SQLite
old = b'        # Fallback: if not found, check payment_metadata["previous_references"]\r\n        # for re-initialized payments where the reference was rotated.\r\n        if not payment:\r\n            payment = (\r\n                db.query(Payment)\r\n                .filter(\r\n                    Payment.payment_metadata["previous_references"].astext.like(\r\n                        f"%{reference}%"\r\n                    )\r\n                )\r\n                .first()\r\n            )'

new = b'        # Fallback: if not found, check payment_metadata["previous_references"]\r\n        # for re-initialized payments where the reference was rotated.\r\n        # Use JSON text search compatible with both SQLite and MySQL.\r\n        if not payment:\r\n            payment = (\r\n                db.query(Payment)\r\n                .filter(\r\n                    Payment.payment_metadata["previous_references"].cast(str).like(\r\n                        f"%{reference}%"\r\n                    )\r\n                )\r\n                .first()\r\n            )'

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'wb') as f:
        f.write(content)
    print('Fixed webhook fallback')
else:
    print('Old string not found')