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
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework import status
from .service import run_extraction
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from core.permissions import HasKeyOrRole
from .serializers import LabelUploadSerializer, LabelItemSerializer , NutritionUOMSerializer , LabelNutritionSerializers
from.models import LabelData , LabelItem , LabelNutrition , NutritionUOM
from rest_framework import generics
from rest_framework.response import Response
from pathlib import Path

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
    parser_class = (MultiPartParser , FormParser)
    def post(self , request , *args , **kwargs):
        label_file = request.data.get('label_file')
        item_id =  request.data.get('item_id')

        if not label_file:
            return Response({"error:  File was not Uplaoded"} , status = status.HTTP_400_BAD_REQUEST)

        serializer = LabelUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"Data is not Valid"} , status = status.HTTP_400_BAD_REQUEST)

        instance = serializer.save()
        label_path = instance.label_file.path

        extracted_data = run_extraction(label_path , item_id)
        instance.parameter_json = extracted_data

        instance.save()

        return Response({
              'file': instance.label_file.name,
              'parameters': instance.parameter_json
          }, status=status.HTTP_201_CREATED)


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

