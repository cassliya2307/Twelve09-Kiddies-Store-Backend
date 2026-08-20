import sys
sys.path.insert(0, r"C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend")

with open(r"C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py", "r") as f:
    content = f.read()

# Find the position: after "    return product" in create_product, before "@app.put"
insert_key = """    return product



@app.post(
    "/products/{product_id}/upload-image",
    response_model=ProductImageUploadResponse,
)
def product_upload_image(
    product_id: int,
    file: ... = ...,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a product image. Admin / product-management only."""
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator or product management permission required",
        )

    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    content = await file.read()
    original_filename = getattr(file, "filename", "unknown")

    from app.services.product_image import validate_and_save
    success, result = validate_and_save(content, original_filename)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result,
        )

    # Update the product's image_url
    old_image_url = product.image_url
    product.image_url = result

    db.add(product)
    db.commit()
    db.refresh(product)

    # If the product previously had a local image and it's different, remove the old file
    if old_image_url and old_image_url != result:
        old_filename = old_image_url.replace("/static/uploads/", "")
        from app.services.product_image import delete_stored
        delete_stored(old_filename)

    from app.schemas.product_image import ProductImageUploadResponse
    return ProductImageUploadResponse(
        image_url=result,
        filename=result.replace("/static/uploads/", ""),
        size_bytes=len(content),
    )


@app.put("/products/{product_id}", response_model=ProductRead)
def update_product"""

replace_str = """    return product



@app.put("/products/{product_id}", response_model=ProductRead)
def update_product"""

if replace_str in content:
    new_content = content.replace(replace_str, insert_key, 1)
    with open(r"C:\Users\DELL\OneDrive\Desktop\Twelve09 Kiddies Store\backend\app\main.py", "w") as f:
        f.write(new_content)
    print("Successfully inserted upload endpoint")
else:
    print("Could not find replacement string")
    # Debug: show what's around line 351
    lines = content.split('\n')
    for i, line in enumerate(lines[348:362], 349):
        print(f'{i}: {line}')