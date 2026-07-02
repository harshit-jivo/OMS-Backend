from django.urls import path
from .views import FeedtoAIView , LabelItemListCreateView , NutritionUOMListCreatView , LabelNutritionListCreateView , LabelItemRetrieveUpdateDestroyView, NutritionUOMRetrieveUpdateDestroyView ,  LabelNutritionRetrieveUpdateDestroyView ,NutrientByItemView


urlpatterns = [
    path('upload/' , FeedtoAIView.as_view()),
    
    path('item/' , LabelItemListCreateView.as_view()),
    path('uom/' , NutritionUOMListCreatView.as_view()),
    path('nutrition/' , LabelNutritionListCreateView.as_view()),
    
    path('item/<int:id>/' , LabelItemRetrieveUpdateDestroyView.as_view()),
    path('uom/<int:id>/' , NutritionUOMRetrieveUpdateDestroyView.as_view()),
    path('nutrition/<int:id>/' , LabelNutritionRetrieveUpdateDestroyView.as_view()),

    path('item-nutrition/' , NutrientByItemView.as_view())
]