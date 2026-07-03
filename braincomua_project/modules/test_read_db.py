# braincomua_project/modules/test_read_db.py

"""
Test script: reads back the dummy record created by test_write_db.py
and prints it, to verify reading through load_django.py works too.
"""
from load_django import *
from parser_app.models import Product

test_product = Product.objects.filter(link="https://example.com/test-product").first()

if test_product:
    print("✅ Record found in database:")
    print(f"  ID:    {test_product.pk}")
    print(f"  Title: {test_product.title}")
    print(f"  Price: {test_product.price}")
else:
    print("❌ Record not found — did you run test_write_db.py first?")