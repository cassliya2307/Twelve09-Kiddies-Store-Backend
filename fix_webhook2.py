with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'rb') as f:
    content = f.read()

# Fix the webhook fallback lookup to work with SQLite
old = b'previous_references"]\n        # for re-initialized payments where the reference was rotated.\n        if not payment:\n            payment = (\n                db.query(Payment)\n                .filter(\n                    Payment.payment_metadata["previous_references"].astext.like(\n                       '

new = b'previous_references"]\n        # for re-initialized payments where the reference was rotated.\n        # Use JSON text search compatible with both SQLite and MySQL.\n        if not payment:\n            payment = (\n                db.query(Payment)\n                .filter(\n                    Payment.payment_metadata["previous_references"].cast(str).like(\n                       '

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'wb') as f:
        f.write(content)
    print('Fixed webhook fallback')
else:
    print('Old string not found')"