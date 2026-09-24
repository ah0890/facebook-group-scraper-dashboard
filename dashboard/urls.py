from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("stats/", views.stats, name="stats"),

    path("groups/", views.group_list, name="groups"),
    path("groups/<int:pk>/edit/", views.group_edit, name="group_edit"),
    path("groups/<int:pk>/delete/", views.group_delete, name="group_delete"),
    path("groups/<int:pk>/toggle/", views.group_toggle, name="group_toggle"),
    path("groups/<int:pk>/run/", views.group_run, name="group_run"),
    path("groups/<int:pk>/test/", views.group_test, name="group_test"),

    path("posts/", views.post_list, name="posts"),
    path("posts/export/csv/", views.export_csv, name="export_csv"),
    path("posts/export/json/", views.export_json, name="export_json"),

    path("run/", views.run_scraper, name="run_scraper"),
    path("run/start/", views.run_start, name="run_start"),
    path("run/stop/", views.run_stop, name="run_stop"),
    path("run/reset-settings/", views.run_reset_settings, name="run_reset_settings"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),

    path("browser/authenticate/", views.browser_authenticate, name="browser_authenticate"),
    path("browser/check/", views.browser_check, name="browser_check"),

    path("logs/", views.logs, name="logs"),
    path("logs/clear/", views.logs_clear, name="logs_clear"),

    path("settings/", views.settings_view, name="settings"),
    path("defaults/", views.defaults_view, name="defaults"),
    path("db/", views.db_view, name="db"),

    path("api/status/", views.api_status, name="api_status"),
]
