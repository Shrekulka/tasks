# braincomua_project/modules/test_write_db.py

"""
Test script: writes one dummy record into the Product model
to verify that load_django.py correctly connects standalone
scripts to Django ORM.
"""
from load_django import *
from parser_app.models import Product

test_product, created = Product.objects.get_or_create(
    link="https://example.com/test-product",
    title="Test Product — load_django check",
    price="9999 грн",
)

if created:
    print("✅ Test record created successfully.")
else:
    print("ℹ️ Test record already existed, nothing new was created.")

print(f"Record ID in database: {test_product.pk}")