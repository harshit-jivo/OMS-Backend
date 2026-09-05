import os

from .models import ComplianceRule , LabelData , LabelItem , NutritionUOM , LabelNutrition
from .service import IMAGE_SUFFIXES
from rest_framework import serializers


class LabelUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabelData
        fields = ['label_file']


class ComplianceRuleSerializer(serializers.ModelSerializer):
    """A rule as the Compliance Rules screen edits it.

    `code` is writable on create and read-only afterwards: reports already
    issued cite it as `rule_id`, so changing it would orphan them. The model's
    `unique=True` gives the create-time collision message for free.
    """

    class Meta:
        model = ComplianceRule
        fields = [
            'id', 'code', 'name', 'rule_text', 'critical_tokens',
            'is_critical', 'is_active', 'sort_order',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def update(self, instance, validated_data):
        # Silently dropped rather than raising: the edit form round-trips the
        # whole object, so a PUT that merely echoes the unchanged code is the
        # normal case, not an error worth rejecting.
        validated_data.pop('code', None)
        return super().update(instance, validated_data)

    def validate_critical_tokens(self, value):
        """A list of non-blank strings, or nothing.

        `JSONField` accepts any JSON, so without this a token list of
        `{"a": 1}` would store fine and then be iterated as keys by
        `service.cross_reference` — a rule that silently checks the wrong
        thing is worse than one that refuses to save.
        """
        if value in (None, ''):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError(
                'Expected a list of strings, e.g. ["Best Before"].')
        tokens = [str(item).strip() for item in value]
        if any(not token for token in tokens):
            raise serializers.ValidationError('Tokens cannot be blank.')
        return tokens
        
class LabelItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabelItem
        fields = '__all__'
        
class NutritionUOMSerializer(serializers.ModelSerializer):
    class Meta:
        model =  NutritionUOM
        fields = '__all__'
        
class LabelNutritionSerializers(serializers.ModelSerializer):
    class Meta:
        model = LabelNutrition
        fields = '__all__'
        
        
        
        
        

class LabelCheckListSerializer(serializers.ModelSerializer):
    """One past check, as the history list shows it.

    Deliberately WITHOUT `findings`: a report is 21 findings with remarks and
    boxes, and a page of 25 of those is a megabyte of JSON to render a list
    of filenames and dates. The summary is what a list needs; the detail
    endpoint has the rest.
    """

    file_name = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    checked_by_name = serializers.SerializerMethodField()
    item_name = serializers.CharField(source='label_item.item_name',
                                      default='', read_only=True)
    summary = serializers.SerializerMethodField()

    class Meta:
        model = LabelData
        fields = ['id', 'file_name', 'image_url', 'uploaded_at',
                  'checked_by_name', 'item_name', 'summary']

    def get_file_name(self, obj):
        return os.path.basename(obj.label_file.name or '')

    def get_image_url(self, obj):
        """A URL a browser can actually put in an `<img>`, or ''.

        The fallback used to be `label_file.url`, which is a PDF for almost
        every check — and a PDF in an `<img>` is a broken image, not a
        fallback. Every row written before `preview_image` existed took that
        path, so the whole history rendered broken thumbnails.

        Three steps, in order of confidence:

        1. the stored preview path;
        2. the path `service.save_preview` WOULD have written, if that file is
           still on disk — true for every historic check here, because the
           previews were always generated, only the path was never recorded;
        3. the upload itself, but ONLY when it is already an image.

        Otherwise '' — and the client shows its own explanation rather than a
        broken image icon.
        """
        if obj.preview_image:
            return obj.preview_image

        name = obj.label_file.name if obj.label_file else ''
        if not name:
            return ''

        from django.core.files.storage import default_storage

        stem = os.path.splitext(os.path.basename(name))[0]
        derived = f'labels/previews/{stem}.png'
        if default_storage.exists(derived):
            return default_storage.url(derived)

        if os.path.splitext(name)[1].lower() in IMAGE_SUFFIXES:
            return obj.label_file.url
        return ''

    def get_checked_by_name(self, obj):
        user = obj.checked_by
        if not user:
            # Historic rows predate attribution, and a blank is honest where
            # invented certainty would not be.
            return ''
        return getattr(user, 'name', '') or user.get_username()

    def get_summary(self, obj):
        return (obj.report_json or {}).get('summary') or {}


class LabelCheckDetailSerializer(LabelCheckListSerializer):
    """A past check, in full — the report the reviewer originally saw."""

    findings = serializers.SerializerMethodField()
    ocr_available = serializers.SerializerMethodField()
    rule_count = serializers.SerializerMethodField()

    class Meta(LabelCheckListSerializer.Meta):
        fields = LabelCheckListSerializer.Meta.fields + [
            'findings', 'ocr_available', 'rule_count',
        ]

    def get_findings(self, obj):
        return (obj.report_json or {}).get('findings') or []

    def get_ocr_available(self, obj):
        # Rows written before this was recorded report None rather than a
        # guess: "we do not know whether OCR ran" is not the same claim as
        # "it did not".
        return (obj.report_json or {}).get('ocr_available')

    def get_rule_count(self, obj):
        report = obj.report_json or {}
        if 'rule_count' in report:
            return report['rule_count']
        return len(report.get('findings') or [])
