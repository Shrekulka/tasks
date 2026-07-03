# braincomua_project/modules/cleanup_empty_json_fields.py

"""
One-off utility: converts existing empty [] / {} values in image_urls and
specifications fields to None, to match the updated model defaults
(default=None instead of default=list/dict). Safe to run multiple times.
"""
from load_django import *
from parser_app.models import Product

updated_images = Product.objects.filter(image_urls=[]).update(image_urls=None)
updated_specs = Product.objects.filter(specifications={}).update(specifications=None)

print(f"✅ Cleaned up {updated_images} record(s) with empty image_urls.")
print(f"✅ Cleaned up {updated_specs} record(s) with empty specifications.")