# Generated for fine-grained APIToken scopes/usage tracking.
#
# Adds scope/resource_paths/label/status/last_used_* columns. Standard AddField
# operations are used so both SQLite (ALTER TABLE ADD COLUMN) and PostgreSQL get
# the real schema. No data migration runs: historical rows keep scope=NULL,
# which is treated as full-admin at auth time and lazily persisted as "admin"
# the first time each token row is updated.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="apitoken",
            name="scope",
            field=models.CharField(
                blank=True,
                choices=[
                    ("read", "Read-only (safe requests: GET/HEAD/OPTIONS)"),
                    ("write", "Read + Write (adds POST/PUT/PATCH)"),
                    ("admin", "Admin (all methods, incl. DELETE and credential management)"),
                ],
                db_index=True,
                default=None,
                help_text="Access tier. Leave blank for legacy tokens (treated as Admin until the token is updated).",
                max_length=16,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="apitoken",
            name="resource_paths",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Optional list of {'path': '/v1-relative/resource', 'methods': ['GET']} allowlist entries.",
            ),
        ),
        migrations.AddField(
            model_name="apitoken",
            name="label",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Human-readable note for this key.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="apitoken",
            name="status",
            field=models.CharField(
                choices=[("active", "Active"), ("revoked", "Revoked")],
                db_index=True,
                default="active",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="apitoken",
            name="last_used_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="apitoken",
            name="last_used_ip",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
    ]
