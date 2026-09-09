from django.urls import path
from .views import LabelCheckHistoryView , LabelCheckDetailView , FeedtoAIView , LabelItemListCreateView , NutritionUOMListCreatView , LabelNutritionListCreateView , LabelItemRetrieveUpdateDestroyView, NutritionUOMRetrieveUpdateDestroyView ,  LabelNutritionRetrieveUpdateDestroyView ,NutrientByItemView , ComplianceRuleListCreateView , ComplianceRuleDetailView


urlpatterns = [
    path('upload/' , FeedtoAIView.as_view()),

    path('history/' , LabelCheckHistoryView.as_view()),
    path('history/<int:id>/' , LabelCheckDetailView.as_view()),

    path('rules/' , ComplianceRuleListCreateView.as_view()),
    path('rules/<int:id>/' , ComplianceRuleDetailView.as_view()),

    path('item/' , LabelItemListCreateView.as_view()),
    path('uom/' , NutritionUOMListCreatView.as_view()),
    path('nutrition/' , LabelNutritionListCreateView.as_view()),
    
    path('item/<int:id>/' , LabelItemRetrieveUpdateDestroyView.as_view()),
    path('uom/<int:id>/' , NutritionUOMRetrieveUpdateDestroyView.as_view()),
    path('nutrition/<int:id>/' , LabelNutritionRetrieveUpdateDestroyView.as_view()),

    path('item-nutrition/' , NutrientByItemView.as_view())
]