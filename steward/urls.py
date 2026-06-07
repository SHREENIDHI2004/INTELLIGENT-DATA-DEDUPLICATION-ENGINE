from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('load-data/', views.load_data, name='load_data'),
    path('find-duplicates/', views.find_duplicates, name='find_duplicates'),
    path('list/', views.record_list, name='record_list'),
    path('review/<str:uid>/', views.review_merge, name='review_merge'),
    path('unmerge/<str:uid>/', views.unmerge_records, name='unmerge_records'),
    path('mark-reviewed/<str:uid>/', views.mark_as_reviewed, name='mark_as_reviewed'),
    path('export/', views.export_data, name='export_data'),
    path('export-duplicates/<str:match_type>/', views.export_duplicates, name='export_duplicates'),
    path('ai-agent-recommend/<str:uid>/', views.ai_agent_recommend, name='ai_agent_recommend'),
]
