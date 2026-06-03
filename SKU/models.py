from django.db import models

# Create your models here.
class SKU(models.Model):
    item_code = models.CharField(max_length=255, unique=True)
    item_name = models.CharField(max_length=255)
    item_image = models.ImageField(upload_to='sku_images/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-uploaded_at']
        db_table = 'sku'
    