from django.db import models

# Create your models here.
class LabelData(models.Model):
    
    label_file = models.FileField(upload_to = 'labels/')
    parameter_json = models.JSONField(null = True , blank = True)
    uploaded_at = models.DateTimeField(auto_now_add = True)
    
    class Meta:
        db_table = 'labels'    
        
        
        
class LabelItem(models.Model):
    
    item_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add = True)
    
    class Meta:
        db_table = 'label_item'
    

class NutritionUOM(models.Model):
    
    uom_name = models.CharField(max_length = 25)
    uom_unit = models.CharField(max_length = 5)
    
    class Meta:
        db_table = 'nutrition_uom'
        
class LabelNutrition(models.Model):
    
    label_item =  models.ForeignKey(LabelItem , on_delete = models.CASCADE)
    uom = models.ForeignKey(NutritionUOM , null = True ,on_delete = models.SET_NULL)
    
    nutrition_name = models.CharField(max_length=125)
    per_serving = models.DecimalField(max_digits = 7 , decimal_places = 3)
    per_100gm = models.DecimalField(max_digits = 7 , decimal_places = 3)

    class Meta:
        db_table = 'label_nutrition'