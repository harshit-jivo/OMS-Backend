"""Nutrition-label extraction and label/nutrition master-data endpoints.

The Phase 2.4 audit made the project default (`IsAuthenticated`) explicit on
every view here rather than invent a tighter gate — the
`RetrieveUpdateDestroyAPIView`s that let ANY signed-in user rewrite label and
nutrition master rows were flagged as the borderline case in the report, not
guessed at.

POLICY CHANGE, deliberate and visible: that decision has now been made. Legal
was the one desk with no grantable permission at all — access existed only as
the `legal` role, so an admin had nothing to tick. Every endpoint now carries
the module's one gate: the `Legal` registry key
(`core/permission_registry.py`), grantable per-user on the Permissions page
or through a role's bundle on the Role Permissions matrix, with the `legal`
role as the transitional fallback. The frontend mirrors it — routeAccess.ts
gates /Label_Checker and /Nutrition_Manager with the same key + role pair.

One key for the whole module, not one per endpoint: the two pages are one
job, the way the `Distributor` grant covers both distributor routes. If the
desks ever split, the new keys belong in the registry with a back-grant, as
the registry's HANA note prescribes for `Reports`.

The role fallback follows the `HasKeyOrRole` cleanup contract, with one
difference from the order endpoints: there is no seed migration granting
`Legal` to the `legal` role's bundle (this database takes no new migrations),
so the fallback stays until the bundle is ticked on the Role Permissions
matrix and verified live. Then every gate here drops to `HasKey('Legal')`.
"""
import logging

from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework import status
from .service import LabelCheckError, run_label_check
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from core.permissions import HasKeyOrRole
from .serializers import (ComplianceRuleSerializer, LabelCheckDetailSerializer,
    LabelCheckListSerializer, LabelUploadSerializer, LabelItemSerializer ,
    NutritionUOMSerializer , LabelNutritionSerializers)
from core.pagination import StandardPagination
from.models import ComplianceRule , LabelData , LabelItem , LabelNutrition , NutritionUOM
from rest_framework import generics
from rest_framework.response import Response
from pathlib import Path

logger = logging.getLogger(__name__)

#: The module's registry key — must match core/permission_registry.py and the
#: frontend's adminPages.ts / routeAccess.ts entries exactly.
LEGAL_PAGE_KEY = 'Legal'


class LegalEndpointGate:
    """The one gate every legal view carries, stated once.

    `HasKeyOrRole` is parameterized, so it is INSTANTIATED in
    `get_permissions()` rather than listed in `permission_classes` — the same
    shape as the order endpoints (orders/views/lifecycle.py). A mixin instead
    of eight copies because the whole module is one desk with one gate; a view
    that ever needs a different rule should declare its own
    `get_permissions()` and say why.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasKeyOrRole(LEGAL_PAGE_KEY, 'legal')]


class FeedtoAIView(LegalEndpointGate, APIView):
    """POST a label (PDF or image) -> the compliance report.

    The response shape is the frontend's contract and is pinned by
    `tests.LabelCheckViewTests`:

        {"file": "labels/x.pdf", "image_url": "/media/labels/previews/x.png",
         "findings": [{"rule_id", "rule_name", "status", "remarks",
                       "ocr_verified"}],
         "summary": {"total", "passed", "failed", "compliant"},
         "ocr_available": bool, "rule_count": int}

    `ocr_text` is deliberately NOT returned. It is stored (for explaining a
    finding later) but it is a page of OCR noise that no reviewer reads, and
    shipping it would triple the payload of every check.

    Errors the user can act on — no rules configured, an unreadable file, the
    model refusing the image — are `LabelCheckError` and come back as 400 with
    that message. Anything else is a real bug and is allowed to 500 rather
    than be flattened into a friendly lie.

    The name is kept (`FeedtoAIView`) because the URL and the frontend already
    point at it; renaming it would be a contract change dressed as a tidy-up.
    """

    parser_classes = (MultiPartParser, FormParser)

    def post(self , request , *args , **kwargs):
        if not request.data.get('label_file'):
            return Response(
                {'success': False, 'message': 'No file was uploaded.'},
                status=status.HTTP_400_BAD_REQUEST)

        serializer = LabelUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'success': False, 'message': 'That file could not be read.',
                 'errors': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST)

        instance = serializer.save()

        try:
            report = run_label_check(instance.label_file.path,
                                     request.data.get('item_id'))
        except LabelCheckError as exc:
            # The upload row stays: a failed check is worth being able to look
            # at afterwards, and the file is already on disk either way.
            logger.warning('Label check failed for %s: %s',
                           instance.label_file.name, exc)
            return Response({'success': False, 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)

        instance.ocr_text = report.get('ocr_text') or ''
        # `ocr_available` and `rule_count` are stored, not just returned: a
        # report reopened from history has to be able to say whether the
        # deterministic half ran, or a reader cannot judge the findings.
        instance.report_json = {
            'findings': report['findings'],
            'summary': report['summary'],
            'ocr_available': report['ocr_available'],
            'rule_count': report['rule_count'],
        }
        instance.preview_image = report.get('preview_url') or ''
        instance.checked_by = request.user if request.user.is_authenticated else None
        item_id = request.data.get('item_id')
        if item_id:
            # Not validated: the check itself already tolerates an unknown
            # item (it simply compares against no nutrition data), and a
            # stale id must not fail a check that otherwise succeeded.
            instance.label_item = LabelItem.objects.filter(id=item_id).first()
        instance.save(update_fields=[
            'ocr_text', 'report_json', 'preview_image', 'checked_by',
            'label_item',
        ])

        return Response({
            'success': True,
            'file': instance.label_file.name,
            # The rasterised preview, not the upload: a PDF cannot be shown in
            # an <img>, and that is the common case. Falls back to the original
            # when the preview could not be written — for an image upload that
            # is still displayable.
            'image_url': report.get('preview_url') or instance.label_file.url,
            'findings': report['findings'],
            'summary': report['summary'],
            'ocr_available': report['ocr_available'],
            'rule_count': report['rule_count'],
        }, status=status.HTTP_201_CREATED)


class ComplianceRuleListCreateView(LegalEndpointGate, generics.ListCreateAPIView):
    """The rule book. Ordering comes from the model's Meta."""

    queryset = ComplianceRule.objects.all()
    serializer_class = ComplianceRuleSerializer


class LabelCheckHistoryView(LegalEndpointGate, generics.ListAPIView):
    """Every past check, newest first.

    The checks were always stored; this is what makes them reachable. It
    matters beyond convenience: a compliance report is a record of what was
    reviewed and when, and one that can only be seen once is not a record.

    Paginated by DEFAULT, unlike the rest of the app's list endpoints. Those
    use `OptInPagination` because they have live clients that index straight
    into a JSON array, and reshaping the response would break them. This
    endpoint is new and has no such client, so it can start where that class's
    own docstring says the destination is.

    Filters: `?item=<id>` and `?failed_only=1`. Both are things a reviewer
    actually asks for — "what did we check for this product" and "what has
    ever failed" — and neither can be answered from a page of 25 rows.
    """

    serializer_class = LabelCheckListSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        queryset = (LabelData.objects
                    .select_related('checked_by', 'label_item')
                    # Rows from the previous pipeline have no report to open,
                    # so listing them would offer a link to a blank page.
                    .filter(report_json__isnull=False))

        item = self.request.query_params.get('item')
        if item:
            queryset = queryset.filter(label_item_id=item)

        if self.request.query_params.get('failed_only') in ('1', 'true', 'True'):
            # A JSON containment query rather than a denormalised column: the
            # summary is written once and read rarely, and a second copy of
            # "did it pass" is a second thing that can disagree.
            queryset = queryset.filter(report_json__summary__compliant=False)

        return queryset


class LabelCheckDetailView(LegalEndpointGate, generics.RetrieveAPIView):
    """One past check, with its findings and highlight boxes.

    The regions stored on a finding are fractions of the image, so reopening a
    check renders exactly the highlights the reviewer saw at the time — even
    though the locator has since changed. That is the right behaviour for a
    record: it shows what was reported, not what today's code would report.
    """

    queryset = LabelData.objects.select_related('checked_by', 'label_item')
    serializer_class = LabelCheckDetailSerializer
    lookup_field = 'id'


class ComplianceRuleDetailView(LegalEndpointGate, generics.RetrieveUpdateDestroyAPIView):
    """One rule.

    DELETE is offered, but deactivating is nearly always what is wanted:
    reports already issued cite `code`, and a deleted rule makes them
    unexplainable. The model's `is_active` field carries that advice.
    """

    queryset = ComplianceRule.objects.all()
    serializer_class = ComplianceRuleSerializer
    lookup_field = 'id'


class LabelItemListCreateView(LegalEndpointGate, generics.ListCreateAPIView):
    queryset = LabelItem.objects.all()
    serializer_class =  LabelItemSerializer

class NutritionUOMListCreatView(LegalEndpointGate, generics.ListCreateAPIView):
    queryset = NutritionUOM.objects.all()
    serializer_class = NutritionUOMSerializer

class LabelNutritionListCreateView(LegalEndpointGate, generics.ListCreateAPIView):
    queryset = LabelNutrition.objects.all()
    serializer_class = LabelNutritionSerializers

class LabelItemRetrieveUpdateDestroyView(LegalEndpointGate, generics.RetrieveUpdateDestroyAPIView):
    queryset = LabelItem.objects.all()
    serializer_class =  LabelItemSerializer
    lookup_field = 'id'

class NutritionUOMRetrieveUpdateDestroyView(LegalEndpointGate, generics.RetrieveUpdateDestroyAPIView):
    queryset = NutritionUOM.objects.all()
    serializer_class = NutritionUOMSerializer
    lookup_field = 'id'


class LabelNutritionRetrieveUpdateDestroyView(LegalEndpointGate, generics.RetrieveUpdateDestroyAPIView):
    queryset = LabelNutrition.objects.all()
    serializer_class = LabelNutritionSerializers
    lookup_field = 'id'


class NutrientByItemView(LegalEndpointGate, APIView):
    def get(self , request):

        item_id = request.query_params.get('item_id')
        result = LabelNutrition.objects.filter(label_item = item_id)

        serialized_data = LabelNutritionSerializers(result, many=True).data
        print(serialized_data)
        return Response({"nutritional_facts" : serialized_data})

