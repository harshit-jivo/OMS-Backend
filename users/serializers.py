from rest_framework import serializers
from django.db.utils import ProgrammingError
from django.contrib.auth import authenticate
from .models import User, Company, MainGroup, State, UserRole, SchemeProduct, UserState
from orders.models import Categories

class SchemeProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchemeProduct
        fields = ['scheme_id', 'scheme_name', 'state', 'item_code', 'is_active']

class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserRole
        fields = ['id', 'name', 'display_name']

class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ['id', 'name']

class MainGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = MainGroup
        fields = ['id', 'name']

class StateSerializer(serializers.ModelSerializer):
    class Meta:
        model = State
        fields = ['id', 'name', 'code']
 
class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Categories
        fields = ['id', 'category']

class UserSerializer(serializers.ModelSerializer):
    company = serializers.SerializerMethodField()
    main_group = serializers.SerializerMethodField()
    main_groups = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    states = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    role = serializers.CharField(source='role.name', read_only=True)
    role_display = serializers.CharField(source='role.display_name', default= None, read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'name', 'username', 'email', 'phone',
            'role','role_display', 'company', 'main_group','main_groups', 'state', 'states', 'category', 'sub_group', 'is_active', 'password'
        ]

    def get_company(self, obj):
        if not obj.company:
            return None
        return CompanySerializer(obj.company).data

    def get_main_group(self, obj):
        if not obj.main_group:
            return None
        return MainGroupSerializer(obj.main_group).data
    
    def get_main_groups(self, obj):
        groups = obj.main_groups.all()
        if not groups.exists():
            return []
        return MainGroupSerializer(groups, many=True).data

    def get_state(self, obj):
        if not obj.state:
            return None
        return StateSerializer(obj.state).data

    def get_states(self, obj):
        assigned_states = []
        seen_state_ids = set()
        user_states_manager = getattr(obj, 'user_states', None)

        if user_states_manager is None:
            if obj.state:
                return [StateSerializer(obj.state).data]
            return []

        try:
            for user_state in user_states_manager.all():
                state = user_state.state
                if not state or state.id in seen_state_ids:
                    continue
                seen_state_ids.add(state.id)
                assigned_states.append(StateSerializer(state).data)
        except (AttributeError, ProgrammingError):
            assigned_states = []

        if assigned_states:
            return assigned_states

        if obj.state:
            return [StateSerializer(obj.state).data]

        return []
        
    def get_category(self, obj):
        if not obj.category:
            return None
        return CategorySerializer(obj.category).data

class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        user = authenticate(
            username=data.get('username'),
            password=data.get('password')
        )
        if not user:
            raise serializers.ValidationError('Invalid username or password')
        if not user.is_active:
            raise serializers.ValidationError('User account is disabled')
        data['user'] = user
        return data

class CreateUserSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, min_length=6)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    phone = serializers.CharField(max_length=15, required=False, allow_blank=True, allow_null=True)

    # Accept integer IDs for foreign keys
    role = serializers.PrimaryKeyRelatedField(queryset=UserRole.objects.all(), required=False, allow_null=True)
    company = serializers.PrimaryKeyRelatedField(queryset=Company.objects.all(), required=False, allow_null=True)
    main_group = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, allow_null=True)
    state = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, allow_null=True)
    main_groups = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, allow_null=True, many=True)
    states = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, allow_null=True, many=True)
    category = serializers.PrimaryKeyRelatedField(queryset=Categories.objects.all(), required=False, allow_null=True)
    sub_group = serializers.CharField(required=False, allow_blank=True, allow_null=True)



    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError('Username already exists')
        return value    

    def validate_email(self, value):
        if value and User.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already exists')
        return value    
    def create(self, validated_data):
        password = validated_data.pop('password')
        main_groups = validated_data.pop('main_groups', [])
        states_list = validated_data.pop('states', [])

        if main_groups and not validated_data.get('main_group'):
            validated_data['main_group'] = main_groups[0]
        if states_list and not validated_data.get('state'):
            validated_data['state'] = states_list[0]

        user = User.objects.create(**validated_data)
        user.set_password(password)
        user.save()

        if main_groups:
            user.main_groups.set(main_groups)

        if states_list:
            existing_state_ids = set(
                UserState.objects.filter(user=user).values_list('state_id', flat=True)
            )
            for state in states_list:
                if state.id not in existing_state_ids:
                    UserState.objects.create(user=user, state=state)

        return user
    

class UpdateUserSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    username = serializers.CharField(max_length=150, required=False)
    password = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    phone = serializers.CharField(max_length=15, required=False, allow_blank=True, allow_null=True)
    is_active = serializers.BooleanField(required=False)

    role = serializers.PrimaryKeyRelatedField(queryset=UserRole.objects.all(), required=False, allow_null=True)
    company = serializers.PrimaryKeyRelatedField(queryset=Company.objects.all(), required=False, allow_null=True)
    main_group = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, allow_null=True)
    state = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, allow_null=True)
    main_groups = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, many=True)
    states = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, many=True)
    category = serializers.PrimaryKeyRelatedField(queryset=Categories.objects.all(), required=False, allow_null=True)
    sub_group = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def update(self, instance, validated_data):
        main_groups = validated_data.pop('main_groups', None)
        states_list = validated_data.pop('states', None)

        instance.name = validated_data.get('name', instance.name)
      
  
        new_username = validated_data.get('username')
        if new_username and new_username != instance.username:
            if User.objects.filter(username=new_username).exists():
                raise serializers.ValidationError({'username': 'Username already exists'})
            instance.username = new_username

        new_email = validated_data.get('email')
        if new_email and new_email != instance.email:
            if User.objects.filter(email=new_email).exists():
              raise serializers.ValidationError({'email': 'Email already exists'})
            instance.email = new_email
        elif 'email' in validated_data and not new_email:
          instance.email = new_email

        instance.phone = validated_data.get('phone', instance.phone)
       
        for field in ['role', 'company', 'main_group', 'state', 'category', 'sub_group', 'is_active']:
            if field in validated_data:
                setattr(instance, field, validated_data.get(field))

        if main_groups is not None:
            instance.main_groups.set(main_groups)
            if main_groups:
                instance.main_group = main_groups[0]

        if states_list is not None:
            UserState.objects.filter(user=instance).delete()
            for state in states_list:
                UserState.objects.create(user=instance, state=state)
            if states_list:
                instance.state = states_list[0]

        # Agar nawa password ditta gaya hai taan hi update karo
        password = validated_data.get('password')
        if password:
            instance.set_password(password)

        instance.save()
        return instance
   
    
