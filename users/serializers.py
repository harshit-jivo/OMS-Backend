from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from django.db.utils import ProgrammingError
from django.contrib.auth import authenticate
from core.permissions import PRIVILEGED_ROLE_NAMES, is_admin
from .models import User, Company, MainGroup, State, UserRole, SchemeProduct, UserState
from orders.models import Categories


def _run_password_validators(value):
    """Apply AUTH_PASSWORD_VALIDATORS to a password set through the API.

    `CreateUserSerializer` / `UpdateUserSerializer` are plain `Serializer`s that
    call `set_password()` themselves, which does NOT validate — so the project's
    configured validators (length, common-password, numeric-only, similarity)
    were bypassed on every account created through the API. Only the
    serializer's own `min_length=6` applied, which is weaker than the settings.
    """
    if not value:
        return value
    try:
        validate_password(value)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages)) from exc
    return value


def assert_may_assign_roles(request, roles):
    """Refuse to let a non-admin hand out a privileged role.

    Defence in depth behind the view-level `IsAdminRole` guard. The two are not
    redundant: the view guard answers "may you manage accounts at all", this
    answers "may you create an account more powerful than your own". If the
    first is ever loosened — to let a regional manager onboard their own staff,
    say — this is what stops that becoming a path to `admin`.

    `request` may be None (management commands, tests constructing serializers
    directly); the check is skipped then, since there is no requester to judge.
    """
    if request is None:
        return
    wanted = {
        str(getattr(r, 'name', '') or '').strip().lower()
        for r in roles if r is not None
    }
    escalating = wanted & PRIVILEGED_ROLE_NAMES
    if escalating and not is_admin(getattr(request, 'user', None)):
        raise serializers.ValidationError({
            'role': [
                'Only an administrator may assign the '
                f'{", ".join(sorted(escalating))} role.'
            ]
        })

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
    categories = serializers.SerializerMethodField()
    role = serializers.CharField(source='role.name', read_only=True)
    role_display = serializers.CharField(source='role.display_name', default= None, read_only=True)
    # Additional roles (e.g. payment_approver) held alongside the primary one.
    # `roles` is the union of both — clients should check that rather than
    # `role` alone, or a user granted a function role looks like they hold none.
    extra_roles = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(read_only=True)
    # Read-only account flags/timestamps surfaced on the mobile Profile screen.
    # Kept read_only so they can never be set through this serializer.
    is_superuser = serializers.BooleanField(read_only=True)
    is_staff = serializers.BooleanField(read_only=True)
    last_login = serializers.DateTimeField(read_only=True)
    date_joined = serializers.DateTimeField(read_only=True)

    class Meta:
        model = User
        # `password` is deliberately ABSENT. It used to be listed here, and
        # because this is a ModelSerializer that meant every response built from
        # it — including the unauthenticated /auth/users/list/ — serialised each
        # user's PBKDF2 hash. No client ever read it (the edit form blanks the
        # field), so removing it is not a contract change. Passwords are written
        # through CreateUserSerializer / UpdateUserSerializer and never read.
        fields = [
            'id', 'name', 'username', 'email', 'phone',
            'role','role_display', 'extra_roles', 'roles', 'company', 'main_group','main_groups', 'state', 'states', 'category', 'categories', 'sub_group', 'is_active', 'is_superuser', 'is_staff', 'last_login', 'date_joined', 'extra_pages', 'created_at'
        ]

    def get_extra_roles(self, obj):
        return [
            {'id': r.id, 'name': r.name, 'display_name': r.display_name}
            for r in obj.extra_roles.all()
        ]

    def get_roles(self, obj):
        """Every role name the user holds — primary plus extras."""
        return sorted(obj.all_role_names())

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

    def get_categories(self, obj):
        categories = obj.categories.all()
        if not categories.exists():
            if obj.category:
                return [CategorySerializer(obj.category).data]
            return []
        return CategorySerializer(categories, many=True).data

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
    # Additional function roles (payment_approver, deposit_creator, …) held
    # alongside `role`, which stays the user's primary.
    extra_roles = serializers.PrimaryKeyRelatedField(queryset=UserRole.objects.all(), required=False, many=True)
    company = serializers.PrimaryKeyRelatedField(queryset=Company.objects.all(), required=False, allow_null=True)
    main_group = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, allow_null=True)
    state = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, allow_null=True)
    main_groups = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, allow_null=True, many=True)
    states = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, allow_null=True, many=True)
    category = serializers.PrimaryKeyRelatedField(queryset=Categories.objects.all(), required=False, allow_null=True)
    categories = serializers.PrimaryKeyRelatedField(queryset=Categories.objects.all(), required=False, allow_null=True, many=True)
    sub_group = serializers.CharField(required=False, allow_blank=True, allow_null=True)



    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError('Username already exists')
        return value    

    def validate_email(self, value):
        if value and User.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already exists')
        return value

    def validate_password(self, value):
        return _run_password_validators(value)

    def validate(self, data):
        assert_may_assign_roles(
            self.context.get('request'),
            [data.get('role'), *(data.get('extra_roles') or [])],
        )
        return data

    def create(self, validated_data):
        password = validated_data.pop('password')
        main_groups = validated_data.pop('main_groups', [])
        states_list = validated_data.pop('states', [])
        categories_list = validated_data.pop('categories', [])
        # M2M — must be popped before create() and set once the row exists.
        extra_roles = validated_data.pop('extra_roles', [])

        if main_groups and not validated_data.get('main_group'):
            validated_data['main_group'] = main_groups[0]
        if states_list and not validated_data.get('state'):
            validated_data['state'] = states_list[0]
        # Keep the single `category` FK as the primary (first selected) category.
        if categories_list and not validated_data.get('category'):
            validated_data['category'] = categories_list[0]

        user = User.objects.create(**validated_data)
        user.set_password(password)
        user.save()

        if main_groups:
            user.main_groups.set(main_groups)

        if states_list:
            user.states.set(states_list)

        if categories_list:
            user.categories.set(categories_list)

        if extra_roles:
            user.extra_roles.set(extra_roles)

        return user


class UpdateUserSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    username = serializers.CharField(max_length=150, required=False)
    password = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    phone = serializers.CharField(max_length=15, required=False, allow_blank=True, allow_null=True)
    is_active = serializers.BooleanField(required=False)

    role = serializers.PrimaryKeyRelatedField(queryset=UserRole.objects.all(), required=False, allow_null=True)
    extra_roles = serializers.PrimaryKeyRelatedField(queryset=UserRole.objects.all(), required=False, many=True)
    company = serializers.PrimaryKeyRelatedField(queryset=Company.objects.all(), required=False, allow_null=True)
    main_group = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, allow_null=True)
    state = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, allow_null=True)
    main_groups = serializers.PrimaryKeyRelatedField(queryset=MainGroup.objects.all(), required=False, many=True)
    states = serializers.PrimaryKeyRelatedField(queryset=State.objects.all(), required=False, many=True)
    category = serializers.PrimaryKeyRelatedField(queryset=Categories.objects.all(), required=False, allow_null=True)
    categories = serializers.PrimaryKeyRelatedField(queryset=Categories.objects.all(), required=False, many=True)
    sub_group = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_password(self, value):
        # Blank/None means "leave the password alone" here — only a real value
        # is validated. `update()` applies the same rule before set_password().
        return _run_password_validators(value)

    def validate(self, data):
        assert_may_assign_roles(
            self.context.get('request'),
            [data.get('role'), *(data.get('extra_roles') or [])],
        )
        return data

    def update(self, instance, validated_data):
        main_groups = validated_data.pop('main_groups', None)
        states_list = validated_data.pop('states', None)
        categories_list = validated_data.pop('categories', None)
        # None = key absent (leave as-is); [] = explicitly clear all extras.
        extra_roles = validated_data.pop('extra_roles', None)

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

        if extra_roles is not None:
            instance.extra_roles.set(extra_roles)

        if main_groups is not None:
            instance.main_groups.set(main_groups)
            if main_groups:
                instance.main_group = main_groups[0]

        if states_list is not None:
            # .set() applies only the real changes (no churn) and fires a single
            # m2m_changed event, so the audit log records one consolidated row.
            instance.states.set(states_list)
            if states_list:
                instance.state = states_list[0]

        if categories_list is not None:
            instance.categories.set(categories_list)
            # Keep the single `category` FK in sync with the first selected one.
            # An empty list must NOT null a `category` that was sent alongside it:
            # this block runs after the field loop above, so doing so silently wiped
            # the category on every update from a client that posts `categories: []`.
            if categories_list:
                instance.category = categories_list[0]
            elif 'category' not in validated_data:
                instance.category = None

        # Agar nawa password ditta gaya hai taan hi update karo
        password = validated_data.get('password')
        if password:
            instance.set_password(password)

        instance.save()
        return instance
   
    
