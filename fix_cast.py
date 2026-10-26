with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'rb') as f:
    content = f.read()

old = b'Payment.payment_metadata["previous_references"].cast(str).like('
new = b'cast(Payment.payment_metadata["previous_references"], String).like('

if old in content:
    content = content.replace(old, new)
    with open(r'C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py', 'wb') as f:
        f.write(content)
    print('Fixed')
else:
    print('Not found')