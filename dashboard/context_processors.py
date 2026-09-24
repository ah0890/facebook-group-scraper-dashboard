from django.urls import reverse

NAV_ITEMS = [
    # label, url name, icon, url names that highlight this item
    ("Dashboard", "dashboard", "bi-speedometer2", {"dashboard"}),
    ("Stats", "stats", "bi-bar-chart-line", {"stats"}),
    ("Groups", "groups", "bi-people", {"groups", "group_edit"}),
    ("Posts", "posts", "bi-card-text", {"posts"}),
    ("Run Scraper", "run_scraper", "bi-play-circle", {"run_scraper", "run_detail"}),
    ("Logs", "logs", "bi-terminal", {"logs"}),
    ("Settings", "settings", "bi-sliders", {"settings"}),
    ("Defaults", "defaults", "bi-arrow-counterclockwise", {"defaults"}),
]


def app_context(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    current = getattr(getattr(request, "resolver_match", None), "url_name", None)
    nav = [
        {"label": label, "url": reverse(name), "icon": icon, "active": current in names}
        for label, name, icon, names in NAV_ITEMS
    ]
    nav_extra = [
        {"label": "Admin", "url": reverse("admin:index"), "icon": "bi-shield-lock", "active": False},
        {"label": "DB", "url": reverse("db"), "icon": "bi-database", "active": current == "db"},
    ]
    from .models import ScraperSetting

    return {
        "nav_items": nav,
        "nav_extra": nav_extra,
        "scraper_mode": ScraperSetting.get_active().scraper_mode,
    }
