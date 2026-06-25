from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework import status
from .service import run_extraction
from rest_framework.parsers import MultiPartParser, FormParser
from .serializers import LabelUploadSerializer
from.models import LabelData
from rest_framework.response import Response
from pathlib import Path

class FeedtoAIView(APIView):

    parser_class = (MultiPartParser , FormParser)    
    def post(self , request , *args , **kwargs):
        label_file = request.data.get('label_file')
        if not label_file:
            return Response({"error:  File was not Uplaoded"} , status = status.HTTP_400_BAD_REQUEST)

        serializer = LabelUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"Data is not Valid"} , status = status.HTTP_400_BAD_REQUEST)

        instance = serializer.save()
        label_path = instance.label_file.path

        extracted_data = run_extraction(label_path)
        instance.parameter_json = extracted_data

        instance.save()

        return Response({
              'file': instance.label_file.name,
              'parameters': instance.parameter_json
          }, status=status.HTTP_201_CREATED)
          
      