with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'rb') as f:
    content = f.read()

old = b'        # Fallback: if not found, check payment_metadata["previous_references"]\n        # for re-initialized payments where the reference was rotated.\n        if not payment:\n            payment = (\n                db.query(Payment)\n                .filter(\n                    cast(Payment.payment_metadata["previous_references"], String).like(\n                        f"%{reference}%"\n                    )\n                )\n                .first()\n            )'

new = b'        # Fallback: if not found, check payment_metadata text for the reference.\n        # For re-initialized payments where the reference was rotated, the old\n        # reference is stored in payment_metadata["previous_references"].\n        # Use simple text search on the entire JSON column (works for SQLite/MySQL).\n        if not payment:\n            payment = (\n                db.query(Payment)\n                .filter(cast(Payment.payment_metadata, String).like(f"%{reference}%"))\n                .first()\n            )'

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'wb') as f:
        f.write(content)
    print('Fixed webhook fallback')
else:
    print('Old string not found')