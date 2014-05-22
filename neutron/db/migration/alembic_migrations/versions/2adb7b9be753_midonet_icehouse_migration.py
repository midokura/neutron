# Copyright 2014 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

"""midonet_icehouse

Revision ID: 2adb7b9be753
Revises: icehouse
Create Date: 2014-05-24 13:51:11.592826

"""

# revision identifiers, used by Alembic.
revision = '2adb7b9be753'
down_revision = 'icehouse'

# Change to ['*'] if this migration applies to all plugins

migration_for_plugins = [
    'neutron.plugins.midonet.plugin.MidonetPluginV2'
]

from alembic import op
import sqlalchemy as sa

from neutron.db import migration


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.add_column('routers', sa.Column('enable_snat', sa.Boolean(), nullable=False))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.drop_column('routers', 'enable_snat')
