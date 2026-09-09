"""SKU image/master-data endpoints for the product-image workflow.

Phase 2.4 audit: none of these views declared `permission_classes` at all —
they relied solely on the project-wide default (`IsAuthenticated`,
OMS/settings.py). Nothing here is admin-only by current design (this is a
regular staff workflow: upload/browse product images, not org-wide
configuration), so the fix is to make that default explicit rather than
invent a new role restriction. `SKUDetailView` does allow PUT/PATCH/DELETE on
a single SKU, which is the one borderline case here — left on plain
`IsAuthenticated` rather than gated with `core.permissions.IsAdminRole`
because no comparable view in this app already establishes that precedent;
flagged in the Phase 2.4 report as a call for a product decision, not guessed.
"""
from django.shortcuts import render
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.generics import RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser

from .serializers import SKUSerializer
from .models import SKU
from  hana.services.services import SalesOrderService

class SKUCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        serializer = SKUSerializer(data=request.data, context={'request': request})
        
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
class SKUListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        skus = SKU.objects.all()
        serializer = SKUSerializer(
            skus, 
            many=True, 
            context={'request': request} 
        )
        
        return Response(serializer.data)
    
class SKUDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    queryset = SKU.objects.all()
    serializer_class = SKUSerializer
    parser_classes = (MultiPartParser, FormParser)
    lookup_field = 'item_code'

class SKUPendingList(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # 1. Fetch the master list from HANA
        all_skus = SalesOrderService().getFGItems()
        
        # 2. Fetch local SKUs that ACTUALLY have images, and cast to a set for speed
        local_completed_skus = set(
            SKU.objects.exclude(item_image__exact='')
                       .exclude(item_image__isnull=True)
                       .values_list('item_code', flat=True)
        )
        
        # 3. Filter instantly using the set
        pending_skus = [
            sku for sku in all_skus 
            if sku['ItemCode'] not in local_completed_skus
        ]

        return Response(pending_skus)