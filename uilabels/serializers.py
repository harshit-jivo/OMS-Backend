from rest_framework import serializers

from .models import UILabel


class UILabelSerializer(serializers.ModelSerializer):
    """Full representation used by the admin CRUD endpoints.

    `field_key` is write-once: it may be set on create but is read-only on
    update (the model enforces uniqueness; the API forbids renaming a key that
    clients already depend on).
    """

    class Meta:
        model = UILabel
        fields = [
            'id', 'field_key', 'display_name', 'description',
            'is_active', 'is_enabled', 'is_required',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_display_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Display name is required.')
        if len(value) > 100:
            raise serializers.ValidationError(
                'Display name must be at most 100 characters.')
        return value

    def validate_field_key(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Field key is required.')
        return value

    def update(self, instance, validated_data):
        # field_key is immutable once created — clients rely on it as a stable key.
        validated_data.pop('field_key', None)
        return super().update(instance, validated_data)
