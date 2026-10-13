# Persistent run ledger for CrawlSchedule: per-period claim rows plus a
# durable scheduling cursor and auditable skip/error state.

import archivebox.base_models.models
import archivebox.uuid_compat
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def backfill_schedule_cursors(apps, schema_editor):
    """Seed last_planned_run_at from the pre-ledger derived last_run_at.

    Legacy semantics were: latest materialized Crawl created_at for crawl
    schedules, modified_at for update maintenance schedules, falling back to
    template.created_at / schedule.created_at. Seeding the cursor there keeps
    upgrades from triggering an immediate catch-up burst.
    """
    CrawlSchedule = apps.get_model("crawls", "CrawlSchedule")
    db_alias = schema_editor.connection.alias

    for schedule in (
        CrawlSchedule.objects.using(db_alias)
        .select_related("template")
        .iterator(chunk_size=200)
    ):
        config = schedule.config or {}
        if config.get("SCHEDULE_KIND") == "update":
            anchor = schedule.modified_at
        else:
            latest_crawl = (
                schedule.crawl_set.using(db_alias)
                .order_by("-created_at")
                .values_list("created_at", flat=True)
                .first()
            )
            anchor = latest_crawl or getattr(schedule.template, "created_at", None) or schedule.created_at
        CrawlSchedule.objects.using(db_alias).filter(pk=schedule.pk).update(last_planned_run_at=anchor)


class Migration(migrations.Migration):

    dependencies = [
        ('crawls', '0019_crawlschedule_config'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='crawlschedule',
            name='last_error_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_error_code',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_error_message',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_planned_run_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_skip_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_skip_count',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_skip_first_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_skip_last_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='last_skip_reason',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='max_backfill_runs',
            field=models.PositiveSmallIntegerField(blank=True, default=1, help_text='Maximum number of missed periods to run when catching up after downtime. Older periods are recorded as skipped.', validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(100)]),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='missed_runs_skipped',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='crawlschedule',
            name='timezone_name',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='timezone'),
        ),
        migrations.CreateModel(
            name='CrawlScheduleRun',
            fields=[
                ('id', models.UUIDField(default=archivebox.uuid_compat.uuid7, editable=False, primary_key=True, serialize=False, unique=True)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('modified_at', models.DateTimeField(auto_now=True)),
                ('planned_at', models.DateTimeField(db_index=True)),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('status', models.CharField(choices=[('claimed', 'Claimed'), ('succeeded', 'Succeeded'), ('failed', 'Failed'), ('skipped', 'Skipped'), ('cancelled', 'Cancelled')], db_index=True, default='claimed', max_length=15)),
                ('reason', models.CharField(blank=True, default='', max_length=32)),
                ('message', models.TextField(blank=True, default='')),
                ('owner', models.CharField(blank=True, default='', max_length=128)),
                ('source', models.CharField(blank=True, default='', max_length=32)),
                ('run_kind', models.CharField(max_length=16)),
                ('crawl', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_runs', to='crawls.crawl')),
                ('created_by', models.ForeignKey(default=archivebox.base_models.models.get_or_create_system_user_pk, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ('schedule', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='runs', to='crawls.crawlschedule')),
            ],
            options={
                'verbose_name': 'Scheduled Crawl Run',
                'verbose_name_plural': 'Scheduled Crawl Runs',
                'abstract': False,
                'indexes': [models.Index(fields=['schedule', 'status'], name='csrun_sched_status_idx'), models.Index(fields=['status', 'planned_at'], name='csrun_status_planned_idx')],
                'constraints': [models.UniqueConstraint(fields=('schedule', 'planned_at'), name='csrun_sched_planned_uniq')],
            },
        ),
        migrations.RunPython(backfill_schedule_cursors, migrations.RunPython.noop),
    ]
