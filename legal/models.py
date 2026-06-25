from django.db import models

# Create your models here.
class LabelData(models.Model):
    
    label_file = models.FileField(upload_to = 'labels/')
    parameter_json = models.JSONField(null = True , blank = True)
    uploaded_at = models.DateTimeField(auto_now_add = True)
    
    class Meta:
        db_table = 'labels'    