# braincomua_project/parser_app/models.py

"""
Product model for storing parsed data from brain.com.ua.
Fields are ordered from most important (core product info)
to least important (service fields).
"""
from django.db import models


class Product(models.Model):
    # 1. Core data — most important fields
    title = models.CharField(
        max_length=500, 
        null=True, 
        blank=True, 
        verbose_name="Product Title"
    )
    price = models.CharField(
        max_length=100, 
        null=True, 
        blank=True, 
        verbose_name="Regular Price"
    )
    promo_price = models.CharField(
        max_length=100, 
        null=True, 
        blank=True, 
        verbose_name="Promo Price"
    )
    vendor = models.CharField(
        max_length=255, 
        null=True, 
        blank=True, 
        verbose_name="Vendor"
    )
    product_code = models.CharField(
        max_length=100, 
        null=True, 
        blank=True, 
        verbose_name="Product Code (SKU)"
    )

    # 2. Additional product characteristics
    color = models.CharField(
        max_length=100, 
        null=True, 
        blank=True, 
        verbose_name="Color"
    )
    memory = models.CharField(
        max_length=100, 
        null=True, 
        blank=True, 
        verbose_name="Memory"
    )
    reviews_count = models.IntegerField(
        null=True, 
        blank=True, 
        verbose_name="Reviews Count"
    )
    screen_diagonal = models.CharField(
        max_length=100, 
        null=True, 
        blank=True, 
        verbose_name="Screen Diagonal"
    )
    screen_resolution = models.CharField(
        max_length=100, 
        null=True, 
        blank=True, 
        verbose_name="Screen Resolution"
    )

    # 3. Complex/nested data — lists and dictionaries go into JSONField [48]
    image_urls = models.JSONField(
        null=True, 
        blank=True, 
        default=list, 
        verbose_name="Image Gallery URLs"
    )
    specifications = models.JSONField(
        null=True, 
        blank=True, 
        default=dict, 
        verbose_name="Detailed Specifications"
    )

    # 4. Service fields [16]
    link = models.URLField(
        max_length=1000, 
        null=True, 
        blank=True,
        unique=True,
        verbose_name="Product Page Link"
    )
    status = models.CharField(
        max_length=50, 
        default="New", 
        null=True, 
        blank=True, 
        verbose_name="Processing Status"
    )
    scraped_at = models.DateTimeField(
        auto_now_add=True, 
        null=True, 
        blank=True, 
        verbose_name="Scraped At"
    )
    last_updated_at = models.DateTimeField(
        auto_now=True,
        null=True,
        blank=True,
        verbose_name="Last Updated At"
    )

    def __str__(self):
        return self.title or f"Product #{self.pk}"

    class Meta:
        db_table = 'product'
        verbose_name = "Product"
        verbose_name_plural = "Products"
        ordering = ('id',)