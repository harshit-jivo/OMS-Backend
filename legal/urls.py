from django.urls import path
from .views import FeedtoAIView


urlpatterns = [
    path('upload/' , FeedtoAIView.as_view())
]