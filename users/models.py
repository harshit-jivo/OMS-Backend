from django.db import models
from django.contrib.auth.models import AbstractUser

class SchemeProduct(models.Model):
    scheme_id = models.AutoField(primary_key=True)
    state_code = models.CharField(max_length=20, db_index=True,null=True, blank=True)  
    # item_code = models.ForeignKey(
    #     'sap_sync.Product',
    #     on_delete=models.PROTECT,
    #     db_column='item_code',
    #     related_name='scheme_products',
    #     null=True
    # )
    item_code = models.CharField(max_length=100,null=True,blank=True)
    scheme_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True, db_column='isactive')

    class Meta:
        db_table = 'scheme_product'
        ordering = ['scheme_name']

    def __str__(self):
        return self.scheme_name
    
class PartyProductAssignment(models.Model):
    """
    Maps parties to products with pricing
    - One party can have many products
    - Same item_code can exist in different categories (OIL, BEVERAGES, MART)
    - Each party-product combination has its own basic_rate
    """
    
    CATEGORY_CHOICES = [
        ('OIL', 'Oil'),
        ('BEVERAGES', 'Beverages'),
        ('MART', 'Mart'),
    ]
    
    card_code = models.CharField(max_length=50, db_index=True)  # Party identifier
    item_code = models.CharField(max_length=50, db_index=True)  # Product identifier
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    basic_rate = models.DecimalField(max_digits=12, decimal_places=4, default=0.00)  # Price for this party
    
    assigned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    assigned_by = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
         null=True,
        blank=True,
        related_name='party_product_assignments_made'
    )
    is_active = models.BooleanField(default=True)
    is_scheme = models.BooleanField(default=False)

    scheme = models.ForeignKey(
        'users.SchemeProduct',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='scheme_id',
        related_name='party_product_assignments'
    )

    class Meta:
        db_table = 'party_product_assignments'
        unique_together = ['card_code', 'item_code', 'category']
        ordering = ['card_code', 'item_code', 'category']

    def __str__(self):
        return f"{self.card_code} - {self.item_code} ({self.category}) - ₹{self.basic_rate}"
   
   
class UserPartyAssignment(models.Model):
    """Maps users to parties using card_code"""
    CATEGORY_CHOICES = [
        ('OIL', 'Oil'),
        ('BEVERAGES', 'Beverages'),
        ('MART', 'Mart'),
    ]

    user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='party_assignments'
    )
    card_code = models.CharField(max_length=50, db_index=True)
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        db_index=True,
        null=True,
        blank=True,
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assignments_made'
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'user_party_assignments'
        unique_together = ['user', 'card_code', 'category']
        ordering = ['-assigned_at']

    def __str__(self):
        return f"{self.user.username} - {self.card_code} ({self.category or '-'})"

class Company(models.Model):
    name = models.CharField(max_length= 100,unique = True)
    is_active = models.BooleanField(default = True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta: 
        verbose_name_plural = "Companies"
    
    def __str__(self):
        return self.name

class MainGroup(models.Model):
    name = models.CharField(max_length= 35 ,unique = True)
    is_active = models.BooleanField(default = True)
    created_at = models.DateTimeField(auto_now_add = True)
    
    class Meta:
        verbose_name = "Main Group"
        verbose_name_plural = "Main Groups"

    def __str__(self):
        return self.name

class State(models.Model):
    name = models.CharField(max_length= 20,unique = True)
    code = models.CharField(max_length = 10,unique = True)
    is_active = models.BooleanField(default=True) 

    class Meta:
        verbose_name = "State"
        verbose_name_plural = "States"

    def __str__(self):
        return self.name    

class UserState(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        db_column='user_id',
        related_name='user_states'
    )
    state = models.ForeignKey(
        'users.State',
        on_delete=models.CASCADE,
        db_column='state_id',
        related_name='user_states'
    )

    class Meta:
        db_table = 'users_user_states'
        managed = False

    def __str__(self):
        return f"{self.user_id} - {self.state_id}"

class UserRole(models.Model):
    name = models.CharField(max_length=50, unique=True) 
    display_name = models.CharField(max_length=100)       
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'users_role'
        ordering = ['name']

    def __str__(self):
        return self.display_name        

class User(AbstractUser):
    first_name = None
    last_name = None
    
    username = models.CharField(max_length=150, unique=True)
    name = models.CharField(max_length=150)
    email = models.EmailField(max_length=150, blank=True, null=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    
    role = models.ForeignKey(
        'users.UserRole',
        on_delete=models.PROTECT,
        related_name='users',
        null=True,
        blank=True
    )
    
    company = models.ForeignKey(
        'Company',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='users'
    )
    
    main_group = models.ForeignKey(
        'MainGroup',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='users'
    )
    
    state = models.ForeignKey(
        'State',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='users'
    )
    
    category = models.ForeignKey(
        'orders.Categories',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='users'
    )

    # All categories assigned to the user. The single `category` FK above is kept
    # as the primary category (always the first selected category) for backward
    # compatibility; `categories` is the full set used for data scoping.
    categories = models.ManyToManyField(
        'orders.Categories',
        blank=True,
        related_name='m2m_users'
    )

    # Sub group(s) assigned to the user (comma separated). Used for product
    # scoping and rate-approver matching.
    sub_group = models.TextField(blank=True, null=True)

    # Extra admin pages this user is granted access to (list of page keys),
    # in addition to whatever their role already allows. Managed from the
    # admin Permissions page.
    extra_pages = models.JSONField(default=list, blank=True , null = True)

    main_groups = models.ManyToManyField(
        'MainGroup',
        blank=True,
        related_name='m2m_users'
    )

    states = models.ManyToManyField(
        'State',
        blank=True,
        related_name='m2m_users'
    )
    
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    
    date_joined = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    created_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, blank=True, null=True, related_name='created_users'
    )
    updated_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, blank=True, null=True, related_name='updated_users'
    )
    
    class Meta:
        db_table = 'users_user'

    def __str__(self):  
        return self.username


    
