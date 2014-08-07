# Copyright 2011 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
from oslo.config import cfg

from neutron.common import constants
from neutron.common import utils
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants as plugin_constants

LOG = logging.getLogger(__name__)


class DynamicRoutingRpcCallbackMixin(object):
    """A mix-in that enable DR agent rpc support. """

    def sync_routingpeers(self, context, **kwargs):
        host = kwargs.get('host')
        dr_plugin = manager.NeutronManager.get_service_plugins()[
            plugin_constants.DYNAMIC_ROUTING]
        if not dr_plugin:
            LOG.error(_('No plugin for Dynamic Routing registered! Will reply '
                        'to dr agent with empty routingpeer dictionary.'))
            return {}
        elif utils.is_extension_supported(
                dr_plugin, constants.DR_AGENT_SCHEDULER):
            if cfg.CONF.dynamic_routing_auto_schedule:
                dr_plugin.auto_schedule_routingpeers(context, host)

        routingpeer_ids = dr_plugin.list_active_sync_routingpeers_on_dr_agent(
            context, host)

        filters = {'routingpeer_id': [routingpeer_ids]}
        return dr_plugin.get_routingpeers(context, filters=filters)

    def sync_advertisenetworks(self, context, **kwargs):
        host = kwargs.get('host')
        dr_plugin = manager.NeutronManager.get_service_plugins()[
            plugin_constants.DYNAMIC_ROUTING]
        if not dr_plugin:
            LOG.error(_('No plugin for Dynamic Routing registered! Will reply '
                        'to dr agent with empty routingpeer dictionary.'))
            return {}
        elif utils.is_extension_supported(
                dr_plugin, constants.DR_AGENT_SCHEDULER):
            if cfg.CONF.dynamic_routing_auto_schedule:
                dr_plugin.auto_schedule_routinginstances(context, host)

        ri_ids = dr_plugin.list_active_sync_routinginstances_on_dr_agent(
            context, host)
        filters = {'routinginstance_id': [ri_ids],
                   'advertise': [True]}
        routinginstances = dr_plugin.get_routinginstances(context,
                                                          filters=filters)
        subnets = []

        if routinginstances:
            routinginstance = routinginstances[0]
            networks = dr_plugin.list_networks_on_routinginstance(
                context, routinginstance['id'])['networks']
            for network in networks:
                subnets.extend(
                    dr_plugin._get_subnets_by_network(context, network['id']))

            advertise_routes = routinginstance['advertise_routes']
            subnets.extend([advertise_route['advertise_route']
                           for advertise_route in advertise_routes])
        return subnets

    def sync_discoverroutes(self, context, **kwargs):
        host = kwargs.get('host')
        dr_plugin = manager.NeutronManager.get_service_plugins()[
            plugin_constants.DYNAMIC_ROUTING]
        if not dr_plugin:
            LOG.error(_('No plugin for Dynamic Routing registered! Will reply '
                        'to dr agent with empty routingpeer dictionary.'))
            return {}
        elif utils.is_extension_supported(
                dr_plugin, constants.DR_AGENT_SCHEDULER):
            if cfg.CONF.dynamic_routing_auto_schedule:
                dr_plugin.auto_schedule_routinginstances(context, host)

        ri_ids = dr_plugin.list_active_sync_routinginstances_on_dr_agent(
            context, host)
        filters = {'routinginstance_id': [ri_ids],
                   'discover': [True]}
        routinginstances = dr_plugin.get_routinginstances(context,
                                                          filters=filters)
        routers = []
        if routinginstances:
            routinginstance = routinginstances[0]
            network_ids = dr_plugin.list_networks_on_routinginstance(
                context, routinginstance['id'])['networks']
            filter_ports = {'network_id': network_ids}
            port_router_ids = self.get_ports(context,
                                      filters=filter_ports,
                                      fields=['device_id'])
            filter_routers = {'id': port_router_ids} 
            routers = dr_plugin.get_routers(context, filters=filter_routers)
            for router in routers:
                dr_plugin.update_router(context, router['id'],
                                        **kwargs['routes'])
