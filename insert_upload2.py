import sys

# Read the file
with open(r"C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py", "r") as f:
    lines = f.readlines()

# Find the insertion point: after "    return product" at line 350 (1-indexed), which is index 349
# We want to insert the upload endpoint between line 350 and line 352 (which is "@app.put")

new_lines = []
inserted = False
for i, line in enumerate(lines):
    new_lines.append(line)
    # After line index 349 which is "    return product", insert the upload endpoint
    if i == 349 and line.strip() == "return product" and not inserted:
        upload_endpoint = [
            '\n',
            '\n',
            '@app.post(\n',
            '    "/products/{product_id}/upload-image",\n',
            '    response_model=ProductImageUploadResponse,\n',
            ')\n',
            'def product_upload_image(\n',
            '    product_id: int,\n',
            '    file: ... = ...,\n',
            '    current_user: User = Depends(get_current_user),\n',
            '    db: Session = Depends(get_db),\n',
            '):\n',
            '    """Upload a product image. Admin / product-management only."""\n',
            '    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):\n',
            '        raise HTTPException(\n',
            '            status_code=status.HTTP_403_FORBIDDEN,\n',
            '            detail="Administrator or product management permission required",\n',
            '        )\n',
            '\n',
            '    product = db.query(Product).filter(Product.id == product_id).first()\n',
            '    if product is None:\n',
            '        raise HTTPException(\n',
            '            status_code=status.HTTP_404_NOT_FOUND,\n',
            '            detail="Product not found",\n',
            '        )\n',
            '\n',
            '    content = await file.read()\n',
            '    original_filename = getattr(file, "filename", "unknown")\n',
            '\n',
            '    from app.services.product_image import validate_and_save\n',
            '    success, result = validate_and_save(content, original_filename)\n',
            '    if not success:\n',
            '        raise HTTPException(\n',
            '            status_code=status.HTTP_400_BAD_REQUEST,\n',
            '            detail=result,\n',
            '        )\n',
            '\n',
            '    # Update the product\\'s image_url\n',
            '    old_image_url = product.image_url\n',
            '    product.image_url = result\n',
            '\n',
            '    db.add(product)\n',
            '    db.commit()\n',
            '    db.refresh(product)\n',
            '\n',
            '    # If the product previously had a local image and it\\'s different, remove the old file\n',
            '    if old_image_url and old_image_url != result:\n',
            '        old_filename = old_image_url.replace("/static/uploads/", "")\n',
            '        from app.services.product_image import delete_stored\n',
            '        delete_stored(old_filename)\n',
            '\n',
            '    from app.schemas.product_image import ProductImageUploadResponse\n',
            '    return ProductImageUploadResponse(\n',
            '        image_url=result,\n',
            '        filename=result.replace("/static/uploads/", ""),\n',
            '        size_bytes=len(content),\n',
            '    )\n',
            '\n',
        ]
        new_lines.extend(upload_endpoint)
        inserted = True

if not inserted:
    print("WARNING: Could not find insertion point, no changes made")
else:
    # Write back
    with open(r"C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py", "w") as f:
        f.writelines(new_lines)
    print(f"Successfully inserted upload endpoint. Total lines: {len(new_lines)}")