# braincomua_project/parser_app/admin.py

import json
from django import forms
from django.db import models
from django.contrib import admin
from django.utils.safestring import mark_safe

# Imports for configuring import/export
from import_export import resources
from import_export.fields import Field
# Import export action mixins
from import_export.admin import ImportExportModelAdmin, ExportActionMixin

from .models import Product


# ─────────────────────────────────────────────────────────────────────────────
# 1. Resource for exporting data to CSV with Cyrillic characters preserved
# ─────────────────────────────────────────────────────────────────────────────
class ProductResource(resources.ModelResource):
    specifications = models.JSONField(
        null=True,
        blank=True,
        default=None,
        verbose_name="Detailed Specifications"
    )
    image_urls = models.JSONField(
        null=True,
        blank=True,
        default=None,
        verbose_name="Image Gallery URLs"
    )

    class Meta:
        model = Product
        fields = (
            'id', 'title', 'price', 'promo_price', 'vendor',
            'product_code', 'color', 'memory', 'reviews_count',
            'screen_diagonal', 'screen_resolution', 'image_urls',
            'specifications', 'link', 'status', 'scraped_at'
        )

    def dehydrate_specifications(self, product):
        if product.specifications:
            # ensure_ascii=False keeps Cyrillic characters as-is during export
            return json.dumps(product.specifications, ensure_ascii=False)
        return ""

    def dehydrate_image_urls(self, product):
        if product.image_urls:
            return json.dumps(product.image_urls, ensure_ascii=False)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. Widget for editing JSON fields
# ─────────────────────────────────────────────────────────────────────────────
class PrettyJSONWidget(forms.Textarea):
    def format_value(self, value):
        try:
            # Display formatted JSON with a 4-space indent and unescaped Cyrillic characters
            return json.dumps(value, indent=4, ensure_ascii=False)
        except Exception:
            return super().format_value(value)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Configure the Product model layout in the admin panel
# ─────────────────────────────────────────────────────────────────────────────
# Inheriting from ExportActionMixin and ImportExportModelAdmin to support both action buttons and bulk actions
@admin.register(Product)
class ProductAdmin(ExportActionMixin, ImportExportModelAdmin):
    resource_classes = [ProductResource]

    # Optimization: Avoid duplicate SELECT COUNT(*) queries to the DB
    show_full_result_count = False

    formfield_overrides = {
        models.JSONField: {'widget': PrettyJSONWidget},
    }

    # ─────────────────────────────────────────────────────────────────────────
    # Custom bulk actions (Actions)
    # ─────────────────────────────────────────────────────────────────────────
    @admin.action(description='Reset review count for selected products')
    def reset_reviews_count(self, request, queryset):
        """Resets the review count of selected products to 0"""
        updated = queryset.update(reviews_count=0)
        self.message_user(
            request,
            f"Successfully reset review count for {updated} product(s)."
        )

    # Register our custom bulk action (the export action is automatically added by the mixin)
    actions = [reset_reviews_count]

    # ─────────────────────────────────────────────────────────────────────────
    # Helper methods for rendering HTML content
    # ─────────────────────────────────────────────────────────────────────────
    @admin.display(description='Photo')
    def image_thumbnail(self, obj):
        if obj.image_urls and isinstance(obj.image_urls, list) and len(obj.image_urls) > 0:
            first_img = obj.image_urls[0]
            return mark_safe(
                f'<img src="{first_img}" width="45" height="45" style="border-radius: 4px; object-fit: cover; border: 1px solid #ddd;" />')
        return "—"

    @admin.display(description='Source Link')
    def clickable_link(self, obj):
        if obj.link:
            return mark_safe(
                f'<a href="{obj.link}" target="_blank" style="font-weight: bold; color: #15c;">Open original on Brain.com.ua ↗</a>')
        return "—"

    @admin.display(description='Image Gallery')
    def gallery_previews(self, obj):
        if obj.image_urls and isinstance(obj.image_urls, list):
            imgs = "".join(
                f'<a href="{url}" target="_blank">'
                f'<img src="{url}" width="100" height="100" style="margin: 5px; border-radius: 6px; border: 1px solid #ddd; object-fit: cover; cursor: zoom-in;" />'
                f'</a>'
                for url in obj.image_urls
            )
            return mark_safe(f'<div style="display: flex; flex-wrap: wrap;">{imgs}</div>')
        return "—"

    @admin.display(description='Product Specifications (Table)')
    def specs_table(self, obj):
        if obj.specifications and isinstance(obj.specifications, dict):
            rows = "".join(
                f'<tr style="border-bottom: 1px solid #eee;">'
                f'<td style="padding: 6px 12px; font-weight: bold; width: 250px; background: #fafafa; color: #555;">{k}</td>'
                f'<td style="padding: 6px 12px; color: #222;">{v}</td>'
                f'</tr>'
                for k, v in obj.specifications.items()
            )
            return mark_safe(
                f'<table style="width: 100%; border-collapse: collapse; border: 1px solid #eee; font-size: 13px;">{rows}</table>')
            return "—"

    readonly_fields = ('image_thumbnail', 'clickable_link', 'gallery_previews', 'specs_table', 'scraped_at')

    # Configure list display, filters, and search fields
    list_display = (
        'image_thumbnail',
        'product_code',
        'title',
        'vendor',
        'price',
        'promo_price',
        'reviews_count',
        'scraped_at'
    )

    list_display_links = ('image_thumbnail', 'product_code', 'title')
    list_filter = ('vendor', 'color', 'memory', 'status', 'scraped_at')
    search_fields = ('title', 'product_code', 'vendor')
    list_editable = ('price', 'promo_price')

    fieldsets = (
        ('Main Information', {
            'fields': ('title', 'vendor', 'product_code', 'clickable_link')
        }),
        ('Appearance and Display', {
            'fields': ('color', 'memory', 'screen_diagonal', 'screen_resolution')
        }),
        ('Pricing and Reviews', {
            'fields': ('price', 'promo_price', 'reviews_count', 'scraped_at')
        }),
        ('Visual Content', {
            'fields': ('gallery_previews',)
        }),
        ('Technical Specifications', {
            'fields': ('specs_table',)
        }),
        ('Automated Parser Metadata (JSON)', {
            'classes': ('collapse',),
            'fields': ('image_urls', 'specifications'),
        }),
    )

