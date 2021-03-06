# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-03-15 19:30
from __future__ import unicode_literals

from django.db import migrations, models
import squad.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_slug_validator'),
    ]

    operations = [
        migrations.AddField(
            model_name='build',
            name='name',
            field=models.CharField(default='', max_length=100),
            preserve_default=False,
        ),
        migrations.RunSQL(
            sql="UPDATE core_build SET name = version;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
