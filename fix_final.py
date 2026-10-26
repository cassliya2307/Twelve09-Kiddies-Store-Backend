with open('tests/test_receipt_workflow.py', 'r') as f:
    content = f.read()

# Fix the problematic line - there might be multiple occurrences
count = 0
while True:
    idx = content.find('"        # Customer 2 tries to upload receipt for customer 1')
    if idx == -1:
        break
    content = content[:idx] + '# Customer 2 tries to upload receipt for customer 1' + content[idx + len('"        # Customer 2 tries to upload receipt for customer 1'):]
    print(f'Fixed one occurrence at position {idx}')

with open('tests/test_receipt_workflow.py', 'w') as f:
    f.write(content)

print('Fixed all occurrences')