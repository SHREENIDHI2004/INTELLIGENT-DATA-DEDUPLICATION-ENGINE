from django.db import models


class Record(models.Model):
    uid = models.CharField(max_length=255, unique=True, verbose_name="UID")
    item_name = models.CharField(max_length=1024, blank=True, null=True, verbose_name="Item_Name")
    location = models.CharField(max_length=255, blank=True, null=True, verbose_name="Location")
    category = models.CharField(max_length=255, blank=True, null=True, verbose_name="Category")
    unit = models.CharField(max_length=255, blank=True, null=True, verbose_name="Unit")
    hsn_code = models.CharField(max_length=255, blank=True, null=True, verbose_name="HSN_Code")
    source_file = models.CharField(max_length=1024, blank=True, null=True, verbose_name="Source_File")
    xref = models.CharField(max_length=255, blank=True, null=True, verbose_name="XREF")

    fuzzy_deduplication_comment = models.TextField(blank=True, null=True, verbose_name="Fuzzy_Deduplication_Comment")
    fuzzy_deduplication_reason = models.TextField(blank=True, null=True, verbose_name="Fuzzy_Deduplication_Reason")
    fuzzy_deduplication_candidates = models.TextField(blank=True, null=True, verbose_name="Fuzzy_Deduplication_Candidates")

    ai_recommendation = models.CharField(max_length=50, blank=True, null=True, verbose_name="AI Recommendation")
    ai_reason = models.TextField(blank=True, null=True, verbose_name="AI Reason")
    ai_confidence = models.CharField(max_length=50, blank=True, null=True, verbose_name="AI Confidence")
    ai_suggested_values = models.TextField(blank=True, null=True, verbose_name="AI Suggested Values (JSON)")

    is_active = models.BooleanField(default=True, help_text="False if merged into another record")
    is_reviewed = models.BooleanField(default=False, help_text="True if record has been reviewed")
    is_merged = models.BooleanField(default=False, help_text="True if record was created/updated via merge")
    merged_info = models.TextField(blank=True, null=True, help_text="Log of merge history")

    @property
    def candidate_count(self):
        if not self.fuzzy_deduplication_candidates:
            return 0
        return len([c.strip() for c in str(self.fuzzy_deduplication_candidates).split(',') if c.strip()])

    def __str__(self):
        return f"{self.uid} - {self.item_name or 'No Item'}"

    class Meta:
        verbose_name = "Data Record"
        verbose_name_plural = "Data Records"
