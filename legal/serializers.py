import os

from . import previews
from .models import ComplianceRule , LabelData , LabelItem , NutritionUOM , LabelNutrition
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
            'id', 'code', 'name', 'rule_text', 'check_type', 'params',
            'critical_tokens', 'is_critical', 'is_active', 'sort_order',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def update(self, instance, validated_data):
        # Silently dropped rather than raising: the edit form round-trips the
        # whole object, so a PUT that merely echoes the unchanged code is the
        # normal case, not an error worth rejecting.
        validated_data.pop('code', None)
        return super().update(instance, validated_data)

    def validate_params(self, value):
        """A JSON object, or nothing.

        Same reasoning as `validate_critical_tokens`: `JSONField` will store a
        list or a bare string quite happily, and `dimensions` reads this with
        `.get()`. A rule whose settings silently do not apply is worse than
        one that refuses to save.
        """
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                'Expected an object, e.g. {"tolerance_de": 10}.')
        return value

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

        TWO bugs live behind this method, and the second is why it is now one
        line.

        The first: the fallback used to be `label_file.url`, which is a PDF
        for almost every check — and a PDF in an `<img>` is a broken image,
        not a fallback. Every row written before `preview_image` existed took
        that path, so the whole history rendered broken thumbnails.

        The second: what it returned was a MEDIA url, and `OMS/urls.py` serves
        `MEDIA_URL` only under `DEBUG`. So the fix above worked on a
        developer's machine and the deployed build showed the broken icon
        anyway, for a different reason. It now names the authenticated view
        that streams the bytes; `legal/previews.py` holds the resolution order
        and the reasoning, and the same module is what that view opens, so the
        two cannot disagree about whether a preview exists.
        """
        return previews.image_url(obj)

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
    skipped = serializers.SerializerMethodField()
    package_spec = serializers.SerializerMethodField()

    class Meta(LabelCheckListSerializer.Meta):
        fields = LabelCheckListSerializer.Meta.fields + [
            'findings', 'ocr_available', 'rule_count', 'skipped',
            'package_spec',
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

    def get_skipped(self, obj):
        """Rules that did not apply to this pack, with the reason.

        Empty for every check run before the dimensional rules existed, which
        is the right answer for them: nothing was skipped, because there was
        nothing skippable.
        """
        return (obj.report_json or {}).get('skipped') or []

    def get_package_spec(self, obj):
        """The dimensions the measurement findings were computed from.

        None when the reviewer left the panel alone — and None for every
        historic check, where it means the same thing.
        """
        return (obj.report_json or {}).get('package_spec')
