# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (C) 2012 Midokura Japan K.K.
# Copyright (C) 2013 Midokura PTE LTD
# All Rights Reserved.
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
# @author: Takaaki Suzuki, Midokura Japan KK
# @author: Tomoe Sugihara, Midokura Japan KK
# @author: Ryu Ishimoto, Midokura Japan KK

from midonetclient import api
from oslo.config import cfg
from webob import exc as w_exc

from eventlet.semaphore import Semaphore
from quantum.common import exceptions as q_exc
from quantum.common.utils import find_config_file
from quantum.db import agentschedulers_db
from quantum.db import api as db
from quantum.db import db_base_plugin_v2
from quantum.db import l3_db
from quantum.db import models_v2
from quantum.db import securitygroups_db
from quantum.extensions import securitygroup as ext_sg
from quantum.openstack.common import log as logging
from quantum.plugins.midonet import config
from quantum.plugins.midonet import midonet_lib
from quantum.openstack.common import rpc
from quantum.common import rpc as q_rpc
from quantum.db import agents_db
from quantum.db import dhcp_rpc_base
from quantum.common import topics


LOG = logging.getLogger(__name__)

OS_TENANT_ROUTER_RULE_KEY = 'OS_TENANT_ROUTER_RULE'
OS_FLOATING_IP_RULE_KEY = 'OS_FLOATING_IP'
SNAT_RULE = 'SNAT'
SNAT_RULE_PROPERTY = {OS_TENANT_ROUTER_RULE_KEY: SNAT_RULE}
METADATA_DEFAULT_IP = '169.254.169.254'
PORT_ALLOC_SEM = Semaphore()


class MidonetResourceNotFound(q_exc.NotFound):
    message = _('MidoNet %(resource_type)s %(id)s could not be found')


class MidonetPluginException(q_exc.QuantumException):
    message = _("%(msg)s")


class MidoRpcCallbacks(dhcp_rpc_base.DhcpRpcCallbackMixin):
    RPC_API_VERSION = '1.1'

    def __init__(self):
        pass

    def create_rpc_dispatcher(self):
        '''Get the rpc dispatcher for this manager.

        This a basic implementation that will call the plugin like get_ports
        and handle basic events
        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        '''
        return q_rpc.PluginRpcDispatcher([self,
                                          agents_db.AgentExtRpcCallback()])


class MidonetPluginV2(db_base_plugin_v2.QuantumDbPluginV2,
                      l3_db.L3_NAT_db_mixin,
                      agentschedulers_db.AgentSchedulerDbMixin,
                      securitygroups_db.SecurityGroupDbMixin):
    supported_extension_aliases = ['router', 'security-group']

    def __init__(self):

        # Read config values
        midonet_conf = cfg.CONF.MIDONET
        midonet_uri = midonet_conf.midonet_uri
        admin_user = midonet_conf.username
        admin_pass = midonet_conf.password
        admin_project_id = midonet_conf.project_id
        mode = midonet_conf.mode
        self.provider_router_id = midonet_conf.provider_router_id
        self.provider_router = None

        self.mido_api = api.MidonetApi(midonet_uri, admin_user,
                                       admin_pass,
                                       project_id=admin_project_id)

        # self.provider_router_id should have been set.
        if self.provider_router_id is None:
            msg = _('provider_router_id and metadata_router_id '
                    'should be configured in the plugin config file')
            LOG.exception(msg)
            raise MidonetPluginException(msg=msg)

        self.chain_manager = midonet_lib.ChainManager(self.mido_api)
        self.pg_manager = midonet_lib.PortGroupManager(self.mido_api)
        self.rule_manager = midonet_lib.RuleManager(self.mido_api)
        self.setup_rpc()
        db.configure_db()

    def _get_provider_router(self):
        if self.provider_router is None:
            self.provider_router = self.mido_api.get_router(
                self.provider_router_id)
        return self.provider_router

    def setup_rpc(self):
        # RPC support
        self.topic = topics.PLUGIN
        self.conn = rpc.create_connection(new=True)
        self.callbacks = MidoRpcCallbacks()
        self.dispatcher = self.callbacks.create_rpc_dispatcher()
        self.conn.create_consumer(self.topic, self.dispatcher,
                                  fanout=False)
        # Consume from all consumers in a thread
        self.conn.consume_in_thread()

    def create_subnet(self, context, subnet):
        """Create Quantum subnet.

        Creates a Quantum subnet and a DHCP entry in MidoNet bridge.
        """
        LOG.debug(_("MidonetPluginV2.create_subnet called: subnet=%r"), subnet)

        if subnet['subnet']['ip_version'] == 6:
            raise q_exc.NotImplementedError(
                _("MidoNet doesn't support IPv6."))

        net = super(MidonetPluginV2, self).get_network(
            context, subnet['subnet']['network_id'], fields=None)
        if net['subnets']:
            raise q_exc.NotImplementedError(
                _("MidoNet doesn't support multiple subnets "
                  "on the same network."))

        session = context.session
        with session.begin(subtransactions=True):
            sn_entry = super(MidonetPluginV2, self).create_subnet(context,
                                                                  subnet)
            try:
                bridge = self.mido_api.get_bridge(sn_entry['network_id'])
            except w_exc.HTTPNotFound:
                raise MidonetResourceNotFound(resource_type='Bridge',
                                              id=sn_entry['network_id'])

            gateway_ip = subnet['subnet']['gateway_ip']
            network_address, prefix = subnet['subnet']['cidr'].split('/')
            bridge.add_dhcp_subnet().default_gateway(gateway_ip).subnet_prefix(
                network_address).subnet_length(prefix).create()

            # If the network is external, link the bridge to MidoNet provider
            # router
            self._extend_network_dict_l3(context, net)
            if net['router:external']:
                gateway_ip = sn_entry['gateway_ip']
                network_address, length = sn_entry['cidr'].split('/')

                # create a interior port in the MidoNet provider router
                in_port = self._get_provider_router().add_interior_port()
                pr_port = in_port.port_address(gateway_ip).network_address(
                    network_address).network_length(length).create()

                # create a interior port in the bridge, then link
                # it to the provider router.
                br_port = bridge.add_interior_port().create()
                pr_port.link(br_port.get_id())

                # add a route for the subnet in the provider router
                self._get_provider_router().add_route().type(
                    'Normal').src_network_addr('0.0.0.0').src_network_length(
                    0).dst_network_addr(
                    network_address).dst_network_length(
                    length).weight(100).next_hop_port(
                    pr_port.get_id()).create()

        LOG.debug(_("MidonetPluginV2.create_subnet exiting: sn_entry=%r"),
                  sn_entry)
        return sn_entry

    def get_subnet(self, context, id, fields=None):
        """Get Quantum subnet.

        Retrieves a Quantum subnet record but also including the DHCP entry
        data stored in MidoNet.
        """
        LOG.debug(_("MidonetPluginV2.get_subnet called: id=%(id)s "
                    "fields=%(fields)s"), {'id': id, 'fields': fields})

        qsubnet = super(MidonetPluginV2, self).get_subnet(context, id)
        bridge_id = qsubnet['network_id']
        try:
            bridge = self.mido_api.get_bridge(bridge_id)
        except w_exc.HTTPNotFound:
            raise MidonetResourceNotFound(resource_type='Bridge',
                                          id=bridge_id)

        # get dhcp subnet data from MidoNet bridge.
        dhcps = bridge.get_dhcp_subnets()
        b_network_address = dhcps[0].get_subnet_prefix()
        b_prefix = dhcps[0].get_subnet_length()

        # Validate against quantum database.
        network_address, prefix = qsubnet['cidr'].split('/')
        if network_address != b_network_address or int(prefix) != b_prefix:
            raise MidonetResourceNotFound(resource_type='DhcpSubnet',
                                          id=qsubnet['cidr'])

        LOG.debug(_("MidonetPluginV2.get_subnet exiting: qsubnet=%s"), qsubnet)
        return qsubnet

    def delete_subnet(self, context, id):
        """Delete Quantum subnet.

        Delete quantum network and its corresponding MidoNet bridge.
        """
        LOG.debug(_("MidonetPluginV2.delete_subnet called: id=%s"), id)

        session = context.session
        with session.begin(subtransactions=True):

            subnet = super(MidonetPluginV2, self).get_subnet(context, id,
                                                             fields=None)
            net = super(MidonetPluginV2, self).get_network(context,
                                                           subnet[
                                                               'network_id'],
                                                           fields=None)
            ret = super(MidonetPluginV2, self).delete_subnet(context, id)
            bridge_id = subnet['network_id']
            try:
                bridge = self.mido_api.get_bridge(bridge_id)
            except w_exc.HTTPNotFound:
                raise MidonetResourceNotFound(resource_type='Bridge',
                                              id=bridge_id)

            dhcp = bridge.get_dhcp_subnets()
            dhcp[0].delete()

            # If the network is external, clean up routes, links, ports.
            self._extend_network_dict_l3(context, net)
            if net['router:external']:
                # Delete routes and unlink the router and the bridge.
                routes = self._get_provider_router().get_routes()

                bridge_ports_to_delete = []
                for p in self._get_provider_router().get_peer_ports():
                    if p.get_device_id() == bridge.get_id():
                        bridge_ports_to_delete.append(p)

                for p in bridge.get_peer_ports():
                    if p.get_device_id() == self._get_provider_router().get_id():
                        # delete the routes going to the brdge
                        for r in routes:
                            if r.get_next_hop_port() == p.get_id():
                                r.delete()
                        p.unlink()
                        p.delete()

                # delete bridge port
                map(lambda x: x.delete(), bridge_ports_to_delete)

        LOG.debug(_("MidonetPluginV2.delete_subnet exiting"))
        return ret

    def create_network(self, context, network):
        """Create Quantum network.

        Create a new Quantum network and its corresponding MidoNet bridge.
        """
        LOG.debug(_('MidonetPluginV2.create_network called: network=%r'),
                  network)

        if network['network']['admin_state_up'] is False:
            LOG.warning(_('Ignoring admin_state_up=False for network=%r'
                          'Overriding with True'), network)
            network['network']['admin_state_up'] = True

        tenant_id = self._get_tenant_id_for_create(context, network['network'])

        self._ensure_default_security_group(context, tenant_id)

        session = context.session
        with session.begin(subtransactions=True):
            bridge = self.mido_api.add_bridge().name(
                network['network']['name']).tenant_id(tenant_id).create()

            # Set MidoNet bridge ID to the quantum DB entry
            network['network']['id'] = bridge.get_id()
            net = super(MidonetPluginV2, self).create_network(context, network)

            # to handle l3 related data in DB
            self._process_l3_create(context, network['network'], net['id'])
            self._extend_network_dict_l3(context, net)
        LOG.debug(_("MidonetPluginV2.create_network exiting: net=%r"), net)
        return net

    def update_network(self, context, id, network):
        """Update Quantum network.

        Update an existing Quantum network and its corresponding MidoNet
        bridge.
        """
        LOG.debug(_("MidonetPluginV2.update_network called: id=%(id)r, "
                    "network=%(network)r"), {'id': id, 'network': network})

        # Reject admin_state_up=False
        if network['network'].get('admin_state_up') and network['network'][
                'admin_state_up'] is False:
            raise q_exc.NotImplementedError(_('admin_state_up=False '
                                              'networks are not '
                                              'supported.'))

        session = context.session
        with session.begin(subtransactions=True):
            net = super(MidonetPluginV2, self).update_network(
                context, id, network)
            try:
                bridge = self.mido_api.get_bridge(id)
            except w_exc.HTTPNotFound:
                raise MidonetResourceNotFound(resource_type='Bridge', id=id)
            bridge.name(net['name']).update()

        self._extend_network_dict_l3(context, net)
        LOG.debug(_("MidonetPluginV2.update_network exiting: net=%r"), net)
        return net

    def get_network(self, context, id, fields=None):
        """Get Quantum network.

        Retrieves a Quantum network and its corresponding MidoNet bridge.
        """
        LOG.debug(_("MidonetPluginV2.get_network called: id=%(id)r, "
                    "fields=%(fields)r"), {'id': id, 'fields': fields})

        # NOTE: Get network data with all fields (fields=None) for
        #       _extend_network_dict_l3() method, which needs 'id' field
        qnet = super(MidonetPluginV2, self).get_network(context, id, None)
        try:
            self.mido_api.get_bridge(id)
        except w_exc.HTTPNotFound:
            raise MidonetResourceNotFound(resource_type='Bridge', id=id)

        self._extend_network_dict_l3(context, qnet)
        LOG.debug(_("MidonetPluginV2.get_network exiting: qnet=%r"), qnet)
        return self._fields(qnet, fields)

    def delete_network(self, context, id):
        """Delete a network and its corresponding MidoNet bridge."""
        LOG.debug(_("MidonetPluginV2.delete_network called: id=%r"), id)

        session = context.session
        with session.begin(subtransactions=True):
            super(MidonetPluginV2, self).delete_network(context, id)
            return self.mido_api.get_bridge(id).delete()

    def create_port(self, context, port):
        """Create a L2 port in Quantum/MidoNet."""
        LOG.debug(_("MidonetPluginV2.create_port called: port=%r"), port)

        is_compute_interface = False
        is_dhcp_interface = False
        port_data = port['port']
        # get the bridge and create a port on it.
        try:
            bridge = self.mido_api.get_bridge(port_data['network_id'])
        except w_exc.HTTPNotFound:
            raise MidonetResourceNotFound(resource_type='Bridge',
                                          id=port_data['network_id'])

        device_owner = port_data['device_owner']

        if device_owner.startswith('compute:') or device_owner is '':
            is_compute_interface = True
            bridge_port = bridge.add_exterior_port().create()
        elif device_owner.startswith('network:dhcp'):
            is_dhcp_interface = True
            bridge_port = bridge.add_exterior_port().create()
        elif device_owner == l3_db.DEVICE_OWNER_ROUTER_INTF:
            bridge_port = bridge.add_interior_port().create()
        elif (device_owner == l3_db.DEVICE_OWNER_ROUTER_GW or
              device_owner == l3_db.DEVICE_OWNER_FLOATINGIP):
            # This is a dummy port to make l3_db happy.
            # This will not be used in MidoNet
            bridge_port = bridge.add_interior_port().create()
        else:
            raise q_exc.NotImplementedError(
                _("MidoNet doesn't recognize this owner"))

        if bridge_port:
            # set midonet port id to quantum port id and create a DB record.
            port_data['id'] = bridge_port.get_id()

        port_db_entry = None
        with PORT_ALLOC_SEM:
            session = context.session
            with session.begin(subtransactions=True):
                port_db_entry = super(MidonetPluginV2,
                                      self).create_port(context, port)
                self._extend_port_dict_security_group(context, port_db_entry)
                fixed_ip = port_db_entry['fixed_ips'][0]['ip_address']

        if port_db_entry:
            if is_compute_interface:
                # Create a DHCP entry if needed.
                if 'ip_address' in (port_db_entry['fixed_ips'] or [{}])[0]:
                    # get ip and mac from DB record, assuming one IP address
                    # at most since we only support one subnet per network now.
                    mac = port_db_entry['mac_address']
                    # create dhcp host entry under the bridge.
                    dhcp_subnets = bridge.get_dhcp_subnets()
                    if dhcp_subnets:
                        dhcp_subnets[0].add_dhcp_host().ip_addr(
                            fixed_ip).mac_addr(mac).create()
            elif is_dhcp_interface:
                dhcp_subnets = bridge.get_dhcp_subnets()
                routes = [{'destinationPrefix': METADATA_DEFAULT_IP,
                           'destinationLength': 32,
                           'gatewayAddr': fixed_ip}]
                if dhcp_subnets:
                    dhcp_subnets[0].opt121_routes(routes).update()

        LOG.debug(_("MidonetPluginV2.create_port exiting: port_db_entry=%r"),
                  port_db_entry)
        return port_db_entry

    def update_port(self, context, id, port):
        """Update port."""
        LOG.debug(_("MidonetPluginV2.update_port called: id=%(id)s "
                    "port=%(port)r"), {'id': id, 'port': port})
        return super(MidonetPluginV2, self).update_port(context, id, port)

    def get_port(self, context, id, fields=None):
        """Retrieve port."""
        LOG.debug(_("MidonetPluginV2.get_port called: id=%(id)s "
                    "fields=%(fields)r"), {'id': id, 'fields': fields})

        # get the quantum port from DB.
        port_db_entry = super(MidonetPluginV2, self).get_port(context,
                                                              id, fields)
        self._extend_port_dict_security_group(context, port_db_entry)

        # verify that corresponding port exists in MidoNet.
        try:
            self.mido_api.get_port(id)
        except w_exc.HTTPNotFound:
            raise MidonetResourceNotFound(resource_type='Port', id=id)

        LOG.debug(_("MidonetPluginV2.get_port exiting: port_db_entry=%r"),
                  port_db_entry)
        return port_db_entry

    def get_ports(self, context, filters=None, fields=None):
        """List quantum ports and verify that they exist in MidoNet."""
        LOG.debug(_("MidonetPluginV2.get_ports called: filters=%(filters)s "
                    "fields=%(fields)r"),
                  {'filters': filters, 'fields': fields})
        ports_db_entry = super(MidonetPluginV2, self).get_ports(context,
                                                                filters,
                                                                fields)
        if ports_db_entry:
            try:
                for port in ports_db_entry:
                    self.mido_api.get_port(port['id'])
                    self._extend_port_dict_security_group(context, port)
            except w_exc.HTTPNotFound:
                raise MidonetResourceNotFound(resource_type='Port',
                                              id=port['id'])
        return ports_db_entry

    def delete_port(self, context, id, l3_port_check=True):
        """Delete a quantum port and corresponding MidoNet bridge port."""
        LOG.debug(_("MidonetPluginV2.delete_port called: id=%(id)s "
                    "l3_port_check=%(l3_port_check)r"),
                  {'id': id, 'l3_port_check': l3_port_check})
        # if needed, check to see if this is a port owned by
        # and l3-router.  If so, we should prevent deletion.
        if l3_port_check:
            self.prevent_l3_port_deletion(context, id)

        with PORT_ALLOC_SEM:
            session = context.session
            with session.begin(subtransactions=True):
                port_db_entry = super(MidonetPluginV2, self).get_port(context,
                                                                      id, None)
                bridge = self.mido_api.get_bridge(port_db_entry['network_id'])
                # Clean up dhcp host entry if needed.
                if 'ip_address' in (port_db_entry['fixed_ips'] or [{}])[0]:
                    # get ip and mac from DB record.
                    ip = port_db_entry['fixed_ips'][0]['ip_address']
                    mac = port_db_entry['mac_address']

                    # create dhcp host entry under the bridge.
                    dhcp_subnets = bridge.get_dhcp_subnets()
                    if dhcp_subnets:
                        for dh in dhcp_subnets[0].get_dhcp_hosts():
                            if dh.get_mac_addr() == mac and dh.get_ip_addr() == ip:
                                dh.delete()

                result = super(MidonetPluginV2, self).delete_port(context, id)
                self.mido_api.get_port(id).delete()
                return result

    #
    # L3 APIs.
    #

    def create_router(self, context, router):
        LOG.debug(_("MidonetPluginV2.create_router called: router=%r"), router)

        r = router['router']
        if r['admin_state_up'] is False:
            LOG.warning(_('Ignoring admin_state_up=False for router=%r, '
                          'Overriding with True'), router)
            r['admin_state_up'] = True

        tenant_id = self._get_tenant_id_for_create(context, r)
        with context.session.begin(subtransactions=True):
            mrouter = self.mido_api.add_router().name(
                r['name']).tenant_id(tenant_id).create()

            # pre-generate id so it will be available when
            # configuring external gw port
            router_db = l3_db.Router(id=mrouter.get_id(),
                                     tenant_id=tenant_id,
                                     name=r['name'],
                                     admin_state_up=r['admin_state_up'],
                                     status="ACTIVE")
            context.session.add(router_db)

            # set chains to in/out filters
            chains = self.chain_manager.create_router_chains(tenant_id,
                                                             mrouter.get_id())
            mrouter.inbound_filter_id(
                chains['in'].get_id()).outbound_filter_id(
                chains['out'].get_id()).update()

            has_gw_info = False
            if 'external_gateway_info' in r:
                gw_info = r['external_gateway_info']
                if gw_info is not None:
                    has_gw_info = True
                del r['external_gateway_info']
            if has_gw_info:
                self._update_router_gw_info(context, router_db['id'], gw_info)
                gw_ip = router_db.gw_port.fixed_ips[0].ip_address
                self._set_router_gateway(
                    mrouter, self._get_provider_router(), gw_ip)

            qrouter = self._make_router_dict(router_db)
            LOG.debug(_("MidonetPluginV2.create_router exiting: qrouter=%r"),
                      qrouter)
            return qrouter

    def _set_router_gateway(self, router, gw_router, gw_ip):
        """Set Midonet router uplink gateway

        :param router: ID of the router
        :param gw_router: gateway router to link to
        :param gw_ip: gateway IP address
        """
        LOG.debug(_("MidonetPluginV2.set_router_gateway called: "
                    "router=%(router)s, gw_router=%(gw_router)s, "
                    "gw_ip=%(gw_ip)s"),
                  {'router': router, 'gw_router': gw_router, 'gw_ip': gw_ip})

        # Create a port on the gateway router.
        gw_port = gw_router.add_interior_port().network_address(
            '169.254.255.0').network_length(30).port_address(
            '169.254.255.1').create()

        # Create a port in the tenant router.
        router_port = router.add_interior_port().network_address(
            '169.254.255.0').network_length(30).port_address(
            '169.254.255.2').create()

        # Link them.
        gw_port.link(router_port.get_id())

        # Add a route for gw_ip to bring it down to tenant
        gw_router.add_route().type('Normal').src_network_addr(
            '0.0.0.0').src_network_length(0).dst_network_addr(
            gw_ip).dst_network_length(32).weight(100).next_hop_port(
            gw_port.get_id()).create()

        # Add default route to uplink in the tenant router
        router.add_route().type('Normal').src_network_addr(
            '0.0.0.0').src_network_length(0).dst_network_addr(
            '0.0.0.0').dst_network_length(0).weight(
            100).next_hop_port(router_port.get_id()).create()

        # ADD SNAT(masquerade) rules
        chains = self.chain_manager.get_router_chains(
            router.get_tenant_id(), router.get_id())

        chains['in'].add_rule().nw_dst_address(gw_ip).nw_dst_length(
            32).type('rev_snat').flow_action('accept').in_ports(
            [router_port.get_id()]).properties(
            SNAT_RULE_PROPERTY).position(1).create()

        nat_targets = []
        nat_targets.append(
            {'addressFrom': gw_ip, 'addressTo': gw_ip,
             'portFrom': 1, 'portTo': 65535})

        chains['out'].add_rule().type('snat').flow_action(
            'accept').nat_targets(nat_targets).out_ports(
            [router_port.get_id()]).properties(
            SNAT_RULE_PROPERTY).position(1).create()

    def _clear_router_gateway(self, router):
        # delete the port that is connected to provider router
        for p in router.get_ports():
            if p.get_port_address() == '169.254.255.2':
                peer_port_id = p.get_peer_id()
                p.unlink()
                self.mido_api.get_port(peer_port_id).delete()
                p.delete()

        # delete default route
        for r in router.get_routes():
            if (r.get_dst_network_addr() == '0.0.0.0' and
                r.get_dst_network_length() == 0):
                r.delete()

        # delete SNAT(masquerade) rules
        chains = self.chain_manager.get_router_chains(
            router.get_tenant_id(),
            router.get_id())

        for r in chains['in'].get_rules():
            if OS_TENANT_ROUTER_RULE_KEY in r.get_properties():
                if r.get_properties()[
                    OS_TENANT_ROUTER_RULE_KEY] == SNAT_RULE:
                    r.delete()

        for r in chains['out'].get_rules():
            if OS_TENANT_ROUTER_RULE_KEY in r.get_properties():
                if r.get_properties()[
                    OS_TENANT_ROUTER_RULE_KEY] == SNAT_RULE:
                    r.delete()

    def update_router(self, context, id, router):
        LOG.debug(_("MidonetPluginV2.update_router called: id=%(id)s "
                    "router=%(router)r"), router)

        # Admin state must be up.
        if router['router'].get('admin_state_up') is False:
            raise q_exc.NotImplementedError(_('admin_state_up=False '
                                              'routers are not '
                                              'supported.'))

        op_gateway_set = False
        op_gateway_clear = False

        # figure out which operation it is in
        if ('external_gateway_info' in router['router'] and
                'network_id' in router['router']['external_gateway_info']):
            op_gateway_set = True
        elif ('external_gateway_info' in router['router'] and
              router['router']['external_gateway_info'] == {}):
            op_gateway_clear = True

        # Get the router's gateway port and its IP address.
        qports = super(MidonetPluginV2, self).get_ports(
            context, {'device_id': [id],
                      'device_owner': ['network:router_gateway']})

        assert len(qports) == 1
        qport = qports[0]
        snat_ip = qport['fixed_ips'][0]['ip_address']

        session = context.session
        with session.begin(subtransactions=True):

            qrouter = super(MidonetPluginV2, self).update_router(context, id,
                                                                 router)

            changed_name = router['router'].get('name')
            if changed_name:
                self.mido_api.get_router(id).name(changed_name).update()

            tenant_router = self.mido_api.get_router(id)
            if op_gateway_set:
                self._set_router_gateway(tenant_router,
                                         self._get_provider_router(),
                                         snat_ip)

            if op_gateway_clear:
                self._clear_router_gateway(tenant_router)
                                           
        LOG.debug(_("MidonetPluginV2.update_router exiting: qrouter=%r"),
                  qrouter)
        return qrouter

    def delete_router(self, context, id):
        LOG.debug(_("MidonetPluginV2.delete_router called: id=%s"), id)

        mrouter = self.mido_api.get_router(id)
        tenant_id = mrouter.get_tenant_id()

        session = context.session
        with session.begin(subtransactions=True):
            result = super(MidonetPluginV2, self).delete_router(context, id)

            # If router is linked to provider router, unlink it.
            self._clear_router_gateway(mrouter)

            # delete corresponding chains
            chains = self.chain_manager.get_router_chains(tenant_id,
                                                          mrouter.get_id())
            chains['in'].delete()
            chains['out'].delete()

            # delete the router
            mrouter.delete()

            LOG.debug(_("MidonetPluginV2.delete_router exiting: result=%s"),
                      result)
            return result

    def get_router(self, context, id, fields=None):
        LOG.debug(_("MidonetPluginV2.get_router called: id=%(id)s "
                    "fields=%(fields)r"), {'id': id, 'fields': fields})
        qrouter = super(MidonetPluginV2, self).get_router(context, id, fields)

        try:
            self.mido_api.get_router(id)
        except w_exc.HTTPNotFound:
            raise MidonetResourceNotFound(resource_type='Router', id=id)

        LOG.debug(_("MidonetPluginV2.get_router exiting: qrouter=%r"),
                  qrouter)
        return qrouter

    def get_routers(self, context, filters=None, fields=None):
        LOG.debug(_("MidonetPluginV2.get_routers called: filters=%(filters)s "
                    "fields=%(fields)r"),
                  {'filters': filters, 'fields': fields})

        qrouters = super(MidonetPluginV2, self).get_routers(
            context, filters, fields)
        for qr in qrouters:
            try:
                self.mido_api.get_router(qr['id'])
            except w_exc.HTTPNotFound:
                raise MidonetResourceNotFound(resource_type='Router',
                                              id=qr['id'])
        return qrouters

    def add_router_interface(self, context, router_id, interface_info):
        LOG.debug(_("MidonetPluginV2.add_router_interface called: "
                    "router_id=%(router_id)s "
                    "interface_info=%(interface_info)r"),
                  {'router_id': router_id, 'interface_info': interface_info})

        # TODO(tomoe): handle a case with 'port' in interface_info
        if 'subnet_id' not in interface_info:
            raise MidonetPluginException("No subnet_id provided. Midonet"
                                         "doesn't support this.")

        session = context.session
        with session.begin(subtransactions=True):
            qport = super(MidonetPluginV2, self).add_router_interface(
                context, router_id, interface_info)

            subnet_id = interface_info['subnet_id']
            subnet = self._get_subnet(context, subnet_id)

            gateway_ip = subnet['gateway_ip']
            network_address, length = subnet['cidr'].split('/')

            # Link the router and the bridge port.
            mrouter = self.mido_api.get_router(router_id)
            mrouter_port = mrouter.add_interior_port().port_address(
                gateway_ip).network_address(
                network_address).network_length(length).create()

            mbridge_port = self.mido_api.get_port(qport['port_id'])
            mrouter_port.link(mbridge_port.get_id())

            # Add a route entry to the subnet
            mrouter.add_route().type('Normal').src_network_addr(
                '0.0.0.0').src_network_length(0).dst_network_addr(
                network_address).dst_network_length(length).weight(
                100).next_hop_port(mrouter_port.get_id()).create()
            # Add a route for the metadata server. Not all VM images
            # supports DHCP option 121
            rport_qry = context.session.query(models_v2.Port)
            dhcp_ports = rport_qry.filter_by(
                network_id=subnet.network_id,
                device_owner='network:dhcp').all()

            # Add a route for the Metadata server in the router to forward
            # the packets to the bridge that will send them to the
            # Metadata Proxy. Since we don't support multiple subnets
            # it's ok to take the first fixed_ip

            #check that the port exists
            if dhcp_ports and dhcp_ports[0].fixed_ips:

                mrouter.add_route().type('Normal').src_network_addr(
                    network_address).src_network_length(
                    length).dst_network_addr(
                    METADATA_DEFAULT_IP).dst_network_length(
                    32).next_hop_port(mrouter_port.get_id()).next_hop_gateway(
                    dhcp_ports[0].fixed_ips[0].ip_address).create()
            else:
                LOG.debug("DHCP agent is not working correctly. No port to " +
                          "reach the Metadata server on this network")

            LOG.debug(_("MidonetPluginV2.add_router_interface exiting: "
                        "qport=%r"), qport)
            return qport

    def remove_router_interface(self, context, router_id, interface_info):
        """Remove interior router ports.
        :param context:
        :param router_id:
        :param interface_info:
        """
        LOG.debug(_("MidonetPluginV2.remove_router_interface called: "
                    "router_id=%(router_id)s "
                    "interface_info=%(interface_info)r"),
                  {'router_id': router_id, 'interface_info': interface_info})
        if 'port_id' in interface_info:

            mbridge_port = self.mido_api.get_port(interface_info['port_id'])
            subnet_id = self.get_port(context,
                                      interface_info['port_id']
                                      )['fixed_ips'][0]['subnet_id']

            subnet = self._get_subnet(context, subnet_id)

        if 'subnet_id' in interface_info:

            subnet_id = interface_info['subnet_id']
            subnet = self._get_subnet(context, subnet_id)
            network_id = subnet['network_id']

            # find a quantum port for the network
            rport_qry = context.session.query(models_v2.Port)
            ports = rport_qry.filter_by(
                device_id=router_id,
                device_owner=l3_db.DEVICE_OWNER_ROUTER_INTF,
                network_id=network_id).all()
            network_port = None
            for p in ports:
                if p['fixed_ips'][0]['subnet_id'] == subnet_id:
                    network_port = p
                    break
            assert network_port
            mbridge_port = self.mido_api.get_port(network_port['id'])

        # get network information from subnet data
        network_addr, network_length = subnet['cidr'].split('/')
        network_length = int(network_length)

        # Unlink the router and the bridge.
        mrouter = self.mido_api.get_router(router_id)
        mrouter_port = self.mido_api.get_port(mbridge_port.get_peer_id())
        mrouter_port.unlink()

        # Delete the route for the subnet and for the MD server.
        # They will both have the same next hop port
        n_deleted_routes = 0
        for r in mrouter.get_routes():
            if r.get_next_hop_port() == mrouter_port.get_id():
                r.delete()
                n_deleted_routes += 1
                #break   # commented out due to issue#314
        # there may be other routes using the same next hot port
        assert n_deleted_routes >= 2

        super(MidonetPluginV2, self).remove_router_interface(
            context, router_id, interface_info)
        LOG.debug(_("MidonetPluginV2.remove_router_interface exiting"))

    def _update_fip_assoc(self, context, fip, floatingip_db, external_port):
        LOG.debug(_("MidonetPluginV2._update_fip_assoc called: fip=%(fip)s"
                    "floatingip_db=%(floatingip_db)s, "
                    "external_port=%(external_port)s"), 
                  {'fip': fip, 'floatingip_db': floatingip_db, 
                   'external_port': external_port})

        session = context.session
        with session.begin(subtransactions=True):
            super(MidonetPluginV2, self)._update_fip_assoc(context, fip, 
                floatingip_db, external_port)

            tenant_id = fip['tenant_id']
            floating_address = floatingip_db['floating_ip_address']
            id = floatingip_db['id']

            # Clear the old association if there is one.
            self._clear_midonet_fip_assoc(context, tenant_id, floatingip_db)

            if 'port_id' in fip and fip['port_id']:
                port_id, internal_ip_address, router_id = self.get_assoc_data(
                   context,
                   fip,
                   floatingip_db['floating_network_id'])
    
                fixed_address = internal_ip_address

                tenant_router = self.mido_api.get_router(router_id)
                # find the provider router port that is connected to the tenant
                # of the floating ip
                for p in tenant_router.get_peer_ports():
                    if p.get_device_id() == self._get_provider_router().get_id():
                        pr_port = p

                # get the tenant router port id connected to provider router
                tr_port_id = pr_port.get_peer_id()

                # add a route for the floating ip to bring it to the tenant
                self._get_provider_router().add_route().type(
                    'Normal').src_network_addr('0.0.0.0').src_network_length(
                    0).dst_network_addr(
                    floating_address).dst_network_length(
                    32).weight(100).next_hop_port(
                    pr_port.get_id()).create()

                chains = self.chain_manager.get_router_chains(tenant_id,
                                                              router_id)
                # add dnat/snat rule pair for the floating ip
                nat_targets = []
                nat_targets.append(
                    {'addressFrom': fixed_address, 'addressTo': fixed_address,
                     'portFrom': 0, 'portTo': 0})

                floating_property = {OS_FLOATING_IP_RULE_KEY: id}
                chains['in'].add_rule().nw_dst_address(
                    floating_address).nw_dst_length(32).type(
                    'dnat').flow_action('accept').nat_targets(
                    nat_targets).in_ports([tr_port_id]).position(
                    1).properties(floating_property).create()

                nat_targets = []
                nat_targets.append(
                    {'addressFrom': floating_address,
                     'addressTo': floating_address,
                     'portFrom': 0,
                     'portTo': 0})

                chains['out'].add_rule().nw_src_address(
                    fixed_address).nw_src_length(32).type(
                    'snat').flow_action('accept').nat_targets(
                    nat_targets).out_ports(
                    [tr_port_id]).position(1).properties(
                    floating_property).create()

        LOG.debug(_("MidonetPluginV2._update_fip_assoc exiting"))

    def _clear_midonet_fip_assoc(self, context, tenant_id, floatingip_db):
        """Clears the floating IP's Midonet associations. Idempotent."""
        LOG.debug(_("MidonetPluginV2._clear_midonet_fip_assoc called: "
                    "tenant_id=%(tenant_id)s, floatingip_db=%(floatingip_db)s"),
                  {'tenant_id': tenant_id, 'floatingip_db': floatingip_db})

        router_id = floatingip_db['router_id']
        if not router_id:
            return

        # delete the route for this floating ip
        floating_address = floatingip_db['floating_ip_address']
        for r in self._get_provider_router().get_routes():
            if (r.get_dst_network_addr() == floating_address and
                    r.get_dst_network_length() == 32):
                r.delete()

        # delete snat/dnat rule pair for this floating ip
        chains = self.chain_manager.get_router_chains(tenant_id, router_id)
        LOG.debug(_('chains=%r'), chains)

        for r in chains['in'].get_rules():
            if OS_FLOATING_IP_RULE_KEY in r.get_properties():
                if r.get_properties()[OS_FLOATING_IP_RULE_KEY] == id:
                    LOG.debug(_('deleting rule=%r'), r)
                    r.delete()
                    break

        for r in chains['out'].get_rules():
            if OS_FLOATING_IP_RULE_KEY in r.get_properties():
                if r.get_properties()[OS_FLOATING_IP_RULE_KEY] == id:
                    LOG.debug(_('deleting rule=%r'), r)
                    r.delete()
                    break

        LOG.debug(_("MidonetPluginV2._clear_fip_assoc exiting"))

    #
    # Security groups supporting methods
    #

    def create_security_group(self, context, security_group, default_sg=False):
        """Create chains for Quantum security group."""
        LOG.debug(_("MidonetPluginV2.create_security_group called: "
                    "security_group=%(security_group)s "
                    "default_sg=%(default_sg)s "),
                  {'security_group': security_group, 'default_sg': default_sg})

        sg = security_group.get('security_group')
        tenant_id = self._get_tenant_id_for_create(context, sg)

        with context.session.begin(subtransactions=True):
            sg_db_entry = super(MidonetPluginV2, self).create_security_group(
                context, security_group, default_sg)

            # Create MidoNet chains and portgroup for the SG
            sg_id = sg_db_entry['id']
            sg_name = sg_db_entry['name']
            self.chain_manager.create_for_sg(tenant_id, sg_id, sg_name)
            self.pg_manager.create(tenant_id, sg_id, sg_name)

            LOG.debug(_("MidonetPluginV2.create_security_group exiting: "
                        "sg_db_entry=%r"), sg_db_entry)
            return sg_db_entry

    def delete_security_group(self, context, id):
        """Delete chains for Quantum security group."""
        LOG.debug(_("MidonetPluginV2.delete_security_group called: id=%s"), id)

        with context.session.begin(subtransactions=True):
            sg_db_entry = super(MidonetPluginV2, self).get_security_group(
                context, id)

            if not sg_db_entry:
                raise ext_sg.SecurityGroupNotFound(id=id)

            sg_name = sg_db_entry['name']
            sg_id = sg_db_entry['id']
            tenant_id = sg_db_entry['tenant_id']

            if sg_name == 'default' and not context.is_admin:
                raise ext_sg.SecurityGroupCannotRemoveDefault()

            filters = {'security_group_id': [sg_id]}
            if super(MidonetPluginV2, self)._get_port_security_group_bindings(
                    context, filters):
                raise ext_sg.SecurityGroupInUse(id=sg_id)

            # Delete MidoNet Chains and portgroup for the SG
            self.chain_manager.delete_for_sg(tenant_id, sg_id, sg_name)
            self.pg_manager.delete(tenant_id, sg_id, sg_name)

            return super(MidonetPluginV2, self).delete_security_group(
                context, id)

    def get_security_groups(self, context, filters=None, fields=None,
                            default_sg=False):
        LOG.debug(_("MidonetPluginV2.get_security_groups called: "
                    "filters=%(filters)r fields=%(fields)r"),
                  {'filters': filters, 'fields': fields})
        return super(MidonetPluginV2, self).get_security_groups(
            context, filters, fields, default_sg=default_sg)

    def get_security_group(self, context, id, fields=None, tenant_id=None):
        LOG.debug(_("MidonetPluginV2.get_security_group called: id=%(id)s "
                    "fields=%(fields)r tenant_id=%(tenant_id)s"),
                  {'id': id, 'fields': fields, 'tenant_id': tenant_id})
        return super(MidonetPluginV2, self).get_security_group(context, id,
                                                               fields)

    def create_security_group_rule(self, context, security_group_rule):
        LOG.debug(_("MidonetPluginV2.create_security_group_rule called: "
                    "security_group_rule=%(security_group_rule)r"),
                  {'security_group_rule': security_group_rule})

        with context.session.begin(subtransactions=True):
            rule_db_entry = super(
                MidonetPluginV2, self).create_security_group_rule(
                context, security_group_rule)

            self.rule_manager.create_for_sg_rule(rule_db_entry)
            LOG.debug(_("MidonetPluginV2.create_security_group_rule exiting: "
                        "rule_db_entry=%r"), rule_db_entry)
            return rule_db_entry

    def delete_security_group_rule(self, context, sgrid):
        LOG.debug(_("MidonetPluginV2.delete_security_group_rule called: "
                    "sgrid=%s"), sgrid)

        with context.session.begin(subtransactions=True):
            rule_db_entry = super(MidonetPluginV2,
                                  self).get_security_group_rule(context, sgrid)

            if not rule_db_entry:
                raise ext_sg.SecurityGroupRuleNotFound(id=sgrid)

            self.rule_manager.delete_for_sg_rule(rule_db_entry)
            return super(MidonetPluginV2,
                         self).delete_security_group_rule(context, sgrid)

    def get_security_group_rules(self, context, filters=None, fields=None):
        LOG.debug(_("MidonetPluginV2.get_security_group_rules called: "
                    "filters=%(filters)r fields=%(fields)r"),
                  {'filters': filters, 'fields': fields})
        return super(MidonetPluginV2, self).get_security_group_rules(
            context, filters, fields)

    def get_security_group_rule(self, context, id, fields=None):
        LOG.debug(_("MidonetPluginV2.get_security_group_rule called: "
                    "id=%(id)s fields=%(fields)r"),
                  {'id': id, 'fields': fields})
        return super(MidonetPluginV2, self).get_security_group_rule(
            context, id, fields)
