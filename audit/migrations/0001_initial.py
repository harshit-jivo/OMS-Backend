from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(blank=True, default='', max_length=150)),
                ('page', models.CharField(blank=True, default='', max_length=100)),
                ('action', models.CharField(blank=True, default='', max_length=20)),
                ('record', models.CharField(blank=True, default='', max_length=255)),
                ('field', models.CharField(blank=True, default='', max_length=100)),
                ('old_value', models.TextField(blank=True, default='')),
                ('new_value', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_logs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Audit Log',
                'verbose_name_plural': 'Audit Logs',
                'db_table': 'audit_log',
                'ordering': ['-created_at'],
            },
        ),
    ]
