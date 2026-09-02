"""Nutrition-label extraction and label/nutrition master-data endpoints.

Phase 2.4 audit: none of these views declared `permission_classes` at all —
they relied solely on the project-wide default (`IsAuthenticated`,
OMS/settings.py). None is admin-only by current design (no comparable view in
this app is already gated with `core.permissions.IsAdminRole`, and inventing
that restriction here would silently tighten access rather than document it),
so the fix is to make the existing default explicit per view. The
`RetrieveUpdateDestroyAPIView`s below do allow PUT/PATCH/DELETE on label and
nutrition-UOM master rows — flagged as the borderline case in the Phase 2.4
report rather than guessed at.
"""
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework import status
from .service import run_extraction
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from .serializers import LabelUploadSerializer, LabelItemSerializer , NutritionUOMSerializer , LabelNutritionSerializers
from.models import LabelData , LabelItem , LabelNutrition , NutritionUOM
from rest_framework import generics
from rest_framework.response import Response
from pathlib import Path

class FeedtoAIView(APIView):
    permission_classes = [IsAuthenticated]

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


class LabelItemListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    queryset = LabelItem.objects.all()
    serializer_class =  LabelItemSerializer

class NutritionUOMListCreatView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    queryset = NutritionUOM.objects.all()
    serializer_class = NutritionUOMSerializer

class LabelNutritionListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    queryset = LabelNutrition.objects.all()
    serializer_class = LabelNutritionSerializers

class LabelItemRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    queryset = LabelItem.objects.all()
    serializer_class =  LabelItemSerializer
    lookup_field = 'id'

class NutritionUOMRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    queryset = NutritionUOM.objects.all()
    serializer_class = NutritionUOMSerializer
    lookup_field = 'id'


class LabelNutritionRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    queryset = LabelNutrition.objects.all()
    serializer_class = LabelNutritionSerializers
    lookup_field = 'id'


class NutrientByItemView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self , request):

        item_id = request.query_params.get('item_id')
        result = LabelNutrition.objects.filter(label_item = item_id)

        serialized_data = LabelNutritionSerializers(result, many=True).data
        print(serialized_data)
        return Response({"nutritional_facts" : serialized_data})

