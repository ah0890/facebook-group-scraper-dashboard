from django import forms

from .models import Group, LogLevel, Phase, ScraperRun, ScraperSetting
from .validators import normalize_group_url


class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ["name", "facebook_url", "enabled", "notes"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Bahria Town Lahore Families"}),
            "facebook_url": forms.URLInput(attrs={"placeholder": "https://www.facebook.com/groups/…"}),
            "notes": forms.Textarea(attrs={"rows": 2, "placeholder": "Optional notes"}),
        }
        help_texts = {
            "facebook_url": "Only add groups your account is a member of and is authorised to collect data from.",
        }

    def clean_facebook_url(self):
        url = normalize_group_url(self.cleaned_data["facebook_url"])
        duplicate = Group.objects.filter(facebook_url=url)
        if self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("This group has already been added.")
        return url

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())


class ScraperSettingForm(forms.ModelForm):
    class Meta:
        model = ScraperSetting
        fields = ScraperSetting.CONFIG_FIELDS


class PostFilterForm(forms.Form):
    DATE_FIELDS = [("collected", "Collected"), ("posted", "Posted")]
    SORTS = [
        ("-collected_at", "Newest collected"), ("collected_at", "Oldest collected"),
        ("-post_timestamp", "Newest posted"), ("-likes_count", "Most likes"), ("-comments_count", "Most comments"),
    ]

    q = forms.CharField(required=False, label="Keyword")
    group = forms.ModelChoiceField(queryset=Group.objects.all(), required=False, empty_label="All groups")
    author = forms.CharField(required=False)
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_field = forms.ChoiceField(choices=DATE_FIELDS, required=False, initial="collected")
    sort = forms.ChoiceField(choices=SORTS, required=False)


class LogFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    run = forms.ModelChoiceField(queryset=ScraperRun.objects.all(), required=False, empty_label="All runs")
    group = forms.ModelChoiceField(queryset=Group.objects.all(), required=False, empty_label="All groups")
    level = forms.ChoiceField(choices=[("", "All levels")] + LogLevel.choices, required=False)
    phase = forms.ChoiceField(choices=[("", "All phases")] + Phase.choices, required=False)
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["run"].queryset = ScraperRun.objects.order_by("-id")
        self.fields["run"].label_from_instance = lambda r: f"#{r.pk} · {r.started_at:%Y-%m-%d %H:%M} · {r.status}"


class ClearLogsForm(forms.Form):
    days = forms.IntegerField(min_value=0, max_value=3650, initial=30,
                              help_text="Delete log entries older than this many days (0 = all logs).")
    confirm = forms.BooleanField(required=True, label="I understand these logs will be permanently deleted.")
