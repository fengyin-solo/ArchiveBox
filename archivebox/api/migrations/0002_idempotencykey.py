# Hand-written following 0001_initial.py:
# raw SQL for sqlite (byte-for-byte controlled DDL), and a state-driven
# rebuild for postgres via archivebox.misc.db.rebuild_models_from_migration_state.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone

from archivebox.uuid_compat import uuid7
from archivebox.base_models.models import get_or_create_system_user_pk


INITIAL_SQL = """
                -- Create api_idempotencykey table
                CREATE TABLE IF NOT EXISTS api_idempotencykey (
                    id TEXT PRIMARY KEY NOT NULL,
                    created_at DATETIME NOT NULL,
                    modified_at DATETIME NOT NULL,

                    key VARCHAR(255) NOT NULL,
                    request_method VARCHAR(10) NOT NULL,
                    request_path VARCHAR(255) NOT NULL,
                    request_hash VARCHAR(64) NOT NULL,

                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    status_code INTEGER,
                    response_content_type VARCHAR(255) NOT NULL DEFAULT '',
                    response_body TEXT NOT NULL DEFAULT '',

                    locked_at DATETIME NOT NULL,
                    completed_at DATETIME,
                    expires_at DATETIME,

                    created_by_id INTEGER NOT NULL,

                    FOREIGN KEY (created_by_id) REFERENCES auth_user(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS api_idempotencykey_created_by_id_idx ON api_idempotencykey(created_by_id);
                CREATE INDEX IF NOT EXISTS api_idempotencykey_created_at_idx ON api_idempotencykey(created_at);
                CREATE INDEX IF NOT EXISTS api_idempotencykey_locked_at_idx ON api_idempotencykey(locked_at);
                CREATE INDEX IF NOT EXISTS api_idempotencykey_expires_at_idx ON api_idempotencykey(expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS unique_idempotency_key_per_creator_path
                    ON api_idempotencykey(created_by_id, request_method, request_path, key);
                    """

INITIAL_REVERSE_SQL = """
                DROP INDEX IF EXISTS unique_idempotency_key_per_creator_path;
                DROP TABLE IF EXISTS api_idempotencykey;
                    """


def _run_sqlite_only_sql(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    migrations.RunSQL(sql=INITIAL_SQL, reverse_sql=INITIAL_REVERSE_SQL).database_forwards(
        "api",
        schema_editor,
        None,
        None,
    )


def _run_sqlite_only_sql_reverse(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    migrations.RunSQL(sql=INITIAL_SQL, reverse_sql=INITIAL_REVERSE_SQL).database_backwards(
        "api",
        schema_editor,
        None,
        None,
    )


def _pg_sync_schema(apps, schema_editor):
    from archivebox.misc.db import rebuild_models_from_migration_state

    rebuild_models_from_migration_state(apps, schema_editor, "api", ["IdempotencyKey"])


def _pg_drop_schema(apps, schema_editor):
    from archivebox.misc.db import drop_models_on_postgres

    drop_models_on_postgres(apps, schema_editor, "api", ["IdempotencyKey"])


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(_run_sqlite_only_sql, reverse_code=_run_sqlite_only_sql_reverse),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="IdempotencyKey",
                    fields=[
                        ("id", models.UUIDField(default=uuid7, editable=False, primary_key=True, serialize=False, unique=True)),
                        ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                        ("modified_at", models.DateTimeField(auto_now=True)),
                        ("key", models.CharField(max_length=255)),
                        ("request_method", models.CharField(max_length=10)),
                        ("request_path", models.CharField(max_length=255)),
                        ("request_hash", models.CharField(max_length=64)),
                        (
                            "status",
                            models.CharField(
                                choices=[("pending", "Pending"), ("completed", "Completed")],
                                default="pending",
                                max_length=20,
                            ),
                        ),
                        ("status_code", models.IntegerField(blank=True, null=True)),
                        ("response_content_type", models.CharField(blank=True, default="", max_length=255)),
                        ("response_body", models.TextField(blank=True, default="")),
                        ("locked_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                        ("completed_at", models.DateTimeField(blank=True, null=True)),
                        ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                        (
                            "created_by",
                            models.ForeignKey(
                                db_index=True,
                                default=get_or_create_system_user_pk,
                                on_delete=django.db.models.deletion.CASCADE,
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Idempotency Key",
                        "verbose_name_plural": "Idempotency Keys",
                        "app_label": "api",
                    },
                ),
                migrations.AddConstraint(
                    model_name="idempotencykey",
                    constraint=models.UniqueConstraint(
                        fields=("created_by", "request_method", "request_path", "key"),
                        name="unique_idempotency_key_per_creator_path",
                    ),
                ),
            ],
        ),
        # On non-sqlite backends the raw DDL above is skipped, so the real
        # schema diverges from Django state. Create the table (including the
        # unique constraint) from the post-migration state (no-op on sqlite).
        migrations.RunPython(_pg_sync_schema, reverse_code=_pg_drop_schema),
    ]
