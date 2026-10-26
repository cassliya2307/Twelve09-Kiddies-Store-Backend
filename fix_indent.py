with open('tests/test_receipt_workflow.py', 'r') as f:
    content = f.read()

# Fix the problematic line
content = content.replace(
    '"        # Customer 2 tries to upload receipt for customer 1',
    '# Customer 2 tries to upload receipt for customer 1'
)

with open('tests/test_receipt_workflow.py', 'w') as f:
    f.write(content)

print('Fixed')