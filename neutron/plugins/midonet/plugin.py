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
# @author: Rossella Sblendido, Midokura Japan KK
# @author: Duarte Nunes, Midokura Japan KK

from midonetclient import api
from oslo.config import cfg
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import exc as sao_exc

from neutron.api.v2 import attributes
from neutron.common import constants
from neutron.common import exceptions as n_exc
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import utils
from neutron.db import agents_db
from neutron.db import agentschedulers_db
from neutron.db import api as db
from neutron.db import db_base_plugin_v2
from neutron.db import dhcp_rpc_base
from neutron.db import external_net_db
from neutron.db import l3_db
from neutron.db import l3_gwmode_db
from neutron.db import models_v2
from neutron.db import securitygroups_db
from neutron.extensions import external_net as ext_net
from neutron.extensions import l3
from neutron.extensions import securitygroup as ext_sg
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import rpc
from neutron.plugins.midonet.common import config  # noqa
from neutron.plugins.midonet.common import net_util
from neutron.plugins.midonet import midonet_lib
import time

LOG = logging.getLogger(__name__)

EXTERNAL_GW_INFO = l3.EXTERNAL_GW_INFO

METADATA_DEFAULT_IP = "169.254.169.254/32"
OS_FLOATING_IP_RULE_KEY = 'OS_FLOATING_IP'
OS_SG_RULE_KEY = 'OS_SG_RULE_ID'
OS_TENANT_ROUTER_RULE_KEY = 'OS_TENANT_ROUTER_RULE'
PRE_ROUTING_CHAIN_NAME = "OS_PRE_ROUTING_%s"
PORT_INBOUND_CHAIN_NAME = "OS_PORT_%s_INBOUND"
PORT_OUTBOUND_CHAIN_NAME = "OS_PORT_%s_OUTBOUND"
POST_ROUTING_CHAIN_NAME = "OS_POST_ROUTING_%s"
SG_INGRESS_CHAIN_NAME = "OS_SG_%s_INGRESS"
SG_EGRESS_CHAIN_NAME = "OS_SG_%s_EGRESS"
SG_IP_ADDR_GROUP_NAME = "OS_IPG_%s"
SNAT_RULE = 'SNAT'

ETHERTYPE_ARP = 0x0806
ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86dd


def _get_nat_ips(type, fip):
    """Get NAT IP address information.

    From the route type given, determine the source and target IP addresses
    from the provided floating IP DB object.
    """
    if type == 'pre-routing':
        return fip["floating_ip_address"], fip["fixed_ip_address"]
    elif type == 'post-routing':
        return fip["fixed_ip_address"], fip["floating_ip_address"]
    else:
        raise ValueError(_("Invalid nat_type %s") % type)


def _nat_chain_names(router_id):
    """Get the chain names for NAT.

    These names are used to associate MidoNet chains to the NAT rules
    applied to the router.  For each of these, there are two NAT types,
    'dnat' and 'snat' that are returned as keys, and the corresponding
    chain names as their values.
    """
    pre_routing_name = PRE_ROUTING_CHAIN_NAME % router_id
    post_routing_name = POST_ROUTING_CHAIN_NAME % router_id
    return {'pre-routing': pre_routing_name, 'post-routing': post_routing_name}


def _sg_chain_names(sg_id):
    """Get the chain names for security group.

    These names are used to associate a security group to MidoNet chains.
    There are two names for ingress and egress security group directions.
    """
    ingress = SG_INGRESS_CHAIN_NAME % sg_id
    egress = SG_EGRESS_CHAIN_NAME % sg_id
    return {'ingress': ingress, 'egress': egress}


def _port_chain_names(port_id):
    """Get the chain names for a port.

    These are chains to hold security group chains.
    """
    inbound = PORT_INBOUND_CHAIN_NAME % port_id
    outbound = PORT_OUTBOUND_CHAIN_NAME % port_id
    return {'inbound': inbound, 'outbound': outbound}


def _sg_ip_addr_group_name(sg_id):
    """Get the IP address group name for security group..

    Associates a security group with a MidoNet IP address group.
    """
    return SG_IP_ADDR_GROUP_NAME % sg_id


def _rule_direction(sg_direction):
    """Convert the SG direction to MidoNet direction

    MidoNet terms them 'inbound' and 'outbound' instead of 'ingress' and
    'egress'.  Also, the direction is reversed since MidoNet sees it
    from the network port's point of view, not the VM's.
    """
    if sg_direction == 'ingress':
        return 'outbound'
    elif sg_direction == 'egress':
        return 'inbound'
    else:
        raise ValueError(_("Unrecognized direction %s") % sg_direction)


def _is_router_interface_port(port):
    """Check whether the given port is a router interface port."""
    device_owner = port['device_owner']
    return (device_owner == l3_db.DEVICE_OWNER_ROUTER_INTF)


def _is_router_gw_port(port):
    """Check whether the given port is a router gateway port."""
    device_owner = port['device_owner']
    return (device_owner == l3_db.DEVICE_OWNER_ROUTER_GW)


def _is_fip_port(port):
    """Check whether the given port is a floating ip port."""
    device_owner = port['device_owner']
    return (device_owner == l3_db.DEVICE_OWNER_FLOATINGIP)


def _is_vif_port(port):
    """Check whether the given port is a standard VIF port."""
    return not (_is_dhcp_port(port) or
                _is_fip_port(port) or
                _is_router_gw_port(port) or
                _is_router_interface_port(port))


def _is_dhcp_port(port):
    """Check whether the given port is a DHCP port."""
    device_owner = port['device_owner']
    return device_owner.startswith('network:dhcp')


def _check_resource_exists(func, id, name, raise_exc=False):
    """Check whether the given resource exists in MidoNet data store."""
    try:
        func(id)
    except midonet_lib.MidonetResourceNotFound as exc:
        LOG.error(_("There is no %(name)s with ID %(id)s in MidoNet."),
                  {"name": name, "id": id})
        if raise_exc:
            raise MidonetPluginException(msg=exc)


def _enable_snat_col_if_missing(func):
    """Repair the neutron database.

    Our plugin requires that the routers table have the enable_snat column,
    however it will not be added when initializing the database for havana.
    This is because the migration scripts that update the neutron database
    do not include this column for the midonet plugin. This function allows
    us to update it on the fly.
    """
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sa_exc.OperationalError:
            session = db.get_session()
            session.execute('ALTER TABLE routers ADD COLUMN enable_snat BOOL')
            session.execute('UPDATE routers SET enable_snat=True')
            return func(*args, **kwargs)
    return wrapper


class MidoRpcCallbacks(dhcp_rpc_base.DhcpRpcCallbackMixin):
    RPC_API_VERSION = '1.1'

    def create_rpc_dispatcher(self):
        """Get the rpc dispatcher for this manager.

        This a basic implementation that will call the plugin like get_ports
        and handle basic events
        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        """
        return n_rpc.PluginRpcDispatcher([self,
                                          agents_db.AgentExtRpcCallback()])


class MidonetPluginException(n_exc.NeutronException):
    message = _("%(msg)s")


class MidonetPluginV2(db_base_plugin_v2.NeutronDbPluginV2,
                      external_net_db.External_net_db_mixin,
                      l3_gwmode_db.L3_NAT_db_mixin,
                      agentschedulers_db.DhcpAgentSchedulerDbMixin,
                      securitygroups_db.SecurityGroupDbMixin):

    supported_extension_aliases = ['ext_gw_mode', 'external-net', 'router',
                                   'agent', 'security-group']

    __native_bulk_support = True

    def __init__(self):
        # Read config values
        midonet_conf = cfg.CONF.MIDONET
        midonet_uri = midonet_conf.midonet_uri
        admin_user = midonet_conf.username
        admin_pass = midonet_conf.password
        admin_project_id = midonet_conf.project_id
        self.provider_router_id = midonet_conf.provider_router_id
        self.provider_router = None

        self.mido_api = api.MidonetApi(midonet_uri, admin_user,
                                       admin_pass,
                                       project_id=admin_project_id)
        self.client = midonet_lib.MidoClient(self.mido_api)

        # self.provider_router_id should have been set.
        if self.provider_router_id is None:
            msg = _('provider_router_id should be configured in the plugin '
                    'config file')
            LOG.exception(msg)
            raise MidonetPluginException(msg=msg)

        self.setup_rpc()
        db.configure_db()

    def _get_provider_router(self):
        if self.provider_router is None:
            self.provider_router = self.client.get_router(
                self.provider_router_id)
        return self.provider_router

    def _dhcp_mappings(self, context, fixed_ips, mac):
        for fixed_ip in fixed_ips:
            subnet = self._get_subnet(context, fixed_ip["subnet_id"])
            if subnet["ip_version"] == 6:
                # TODO(ryu) handle IPv6
                continue
            if not subnet["enable_dhcp"]:
                # Skip if DHCP is disabled
                continue
            yield subnet['cidr'], fixed_ip["ip_address"], mac

    def _metadata_subnets(self, context, fixed_ips):
        for fixed_ip in fixed_ips:
            subnet = self._get_subnet(context, fixed_ip["subnet_id"])
            if subnet["ip_version"] == 6:
                continue
            yield subnet['cidr'], fixed_ip["ip_address"]

    def _initialize_port_chains(self, context, port,
                                in_chain, out_chain, sg_ids):
        """Initializes port's security rule chains.

        in_chain - Empty chain to be initialized. This is the chain
        regulating traffic into the network and out of the VM.

        out_chain - Empty chain to be initialized. This is the chain
        regulating traffic out of the network and into the VM.

        sg_ids - IDs of security groups to which the port belongs.
        """

        tenant_id = port["tenant_id"]

        # Inserting a rule at position 1 will insert it at the
        # beginning and push all rules back, so we insert the rules in
        # reverse order.

        # Both chains drop non-ARP traffic if no other rules match.
        for chain in [in_chain, out_chain]:
            self._add_chain_rule(chain, action='drop',
                                 dl_type=ETHERTYPE_ARP,
                                 inv_dl_type=True, position=1)

        # Add reverse flow matching for in_chain.
        self._add_chain_rule(in_chain, action='accept',
                             match_return_flow=True,
                             position=1)

        # MAC spoofing protection for in_chain
        self._add_chain_rule(in_chain, action='drop',
                             dl_src=port["mac_address"], inv_dl_src=True,
                             position=1)

        # IP spoofing protection for in_chain
        for fixed_ip in port["fixed_ips"]:
            subnet = self._get_subnet(context, fixed_ip["subnet_id"])
            if subnet["ip_version"] == 6:
                self._add_chain_rule(in_chain, action="drop", position=1,
                                     src_addr=fixed_ip["ip_address"] + "/128",
                                     inv_nw_src=True, dl_type=ETHERTYPE_IPV6)
            else:
                self._add_chain_rule(in_chain, action="drop", position=1,
                                     src_addr=fixed_ip["ip_address"] + "/32",
                                     inv_nw_src=True, dl_type=ETHERTYPE_IPV4)

        # Add reverse flow matching for out_chain.
        self._add_chain_rule(out_chain, action='accept',
                             match_return_flow=True,
                             position=1)

        # Add jump rules for security groups.
        if sg_ids:
            for sg_id in sg_ids:
                sg_egress, sg_ingress = self._get_sg_chains(tenant_id, sg_id)
                self._add_sg_jumps_to_port_chains(
                    in_chain, out_chain, sg_egress, sg_ingress)

    def _add_sg_jumps_to_port_chains(self, port_inbound, port_outbound,
                                     sg_egress, sg_ingress):
        """Adds jumps from port chains to security group chains.

        Specifically, adds a jump from port_inbound to sg_egress and a
        jump from port_outbound to sg_ingress. The jumps are inserted
        at the penultimate position in each port chain, just before the
        drop rule that drops all non-ARP traffic not allowed by a
        security group's rule.

        port_inbound - Port's chain for traffic into the network and
        out of the VM.

        port_outbound - Port's chain for traffic out of the network and
        in to the VM.

        sg_egress - Security group's chain for traffic out of the VM.
        """

        for port_chain, sg_chain in [(port_inbound, sg_egress),
                                     (port_outbound, sg_ingress)]:
            self._add_chain_rule(port_chain, action='jump',
                                 jump_chain_id=sg_chain.get_id(),
                                 jump_chain_name=sg_chain.get_name(),
                                 position=len(port_chain.get_rules()))

    def _remove_sg_jumps_from_port_chains(self, port):
        """Removes from each of the port's chains (inbound and outbound)
        the jump rules to each of the port's security groups' chains.
        """
        tenant_id = port["tenant_id"]
        port_inbound, port_outbound = self._get_port_chains(port)
        for sg_id in port["security_groups"]:
            sg_egress, sg_ingress = self._get_sg_chains(tenant_id, sg_id)
            for port_chain, sg_chain in [(port_inbound, sg_egress),
                                         (port_outbound, sg_ingress)]:
                for r in port_chain.get_rules():
                    if (r.get_type() == "jump" and
                            r.get_jump_chain_name() == sg_chain.get_name()):
                        self.client.remove_chain_rule(r.get_id())

    def _unbind_port_from_sgs(self, context, port):
        """Unbinds port from all of its security groups. This includes
        deleting the Neutron bindings, removing the port's IP addresses
        from the associated IP address groups, and removing the jump
        rules from the port's rule chains.
        """
        sg_bindings = self._get_port_security_group_bindings(
            context, filters={"port_id": [port["id"]]})
        for sg_binding in sg_bindings:
            sg_id = sg_binding["security_group_id"]
            for ip_addr in port["fixed_ips"]:
                self.client.remove_ip_addr_from_ip_addr_group(
                    ip_addr["ip_address"], sg_id)
        self._delete_port_security_group_bindings(context, port["id"])
        self._remove_sg_jumps_from_port_chains(port)

    def _bind_port_to_sgs(self, context, port, sg_ids):
        self._process_port_create_security_group(context, port, sg_ids)
        if sg_ids is not None:
            tenant_id = port["tenant_id"]
            in_chain, out_chain = self._get_port_chains(port)
            for sg_id in sg_ids:
                for ip_addr in port["fixed_ips"]:
                    self.client.add_ip_addr_to_ip_addr_group(
                        sg_id, ip_addr["ip_address"])

                # If the port chains don't exist yet, the port is still
                # being created, and we can skip adding jump rules now
                # because it will be handled later.
                if in_chain is not None:
                    egress_chain, ingress_chain = self._get_sg_chains(
                        tenant_id, sg_id)
                    self._add_sg_jumps_to_port_chains(
                        in_chain, out_chain, egress_chain, ingress_chain)

    def _get_port_chains(self, port):
        tenant_id = port["tenant_id"]
        chain_names = _port_chain_names(port["id"])
        in_chain = self.client.get_chain_by_name(
            tenant_id, chain_names["inbound"])
        out_chain = self.client.get_chain_by_name(
            tenant_id, chain_names["outbound"])
        return in_chain, out_chain

    def _get_sg_chains(self, tenant_id, sg_id):
        chain_names = _sg_chain_names(sg_id)
        egress_chain = self.client.get_chain_by_name(
            tenant_id, chain_names["egress"])
        ingress_chain = self.client.get_chain_by_name(
            tenant_id, chain_names["ingress"])
        return egress_chain, ingress_chain

    def _create_accept_chain_rule(self, context, sg_rule, chain=None):
        direction = sg_rule["direction"]
        tenant_id = sg_rule["tenant_id"]
        chain_name = _sg_chain_names(sg_rule["security_group_id"])[direction]

        if chain is None:
            chain = self.client.get_chain_by_name(tenant_id, chain_name)

        props = {OS_SG_RULE_KEY: str(sg_rule["id"])}

        # Determine source or destination address by looking at direction
        src_ipg_id = dst_ipg_id = None
        src_addr = dst_addr = None
        src_port_from = None
        src_port_to = None
        dst_port_from = sg_rule["port_range_min"]
        dst_port_to = sg_rule["port_range_max"]
        if direction == "egress":
            dst_ipg_id = sg_rule["remote_group_id"]
            dst_addr = sg_rule["remote_ip_prefix"]
            match_forward_flow = True
        else:
            src_ipg_id = sg_rule["remote_group_id"]
            src_addr = sg_rule["remote_ip_prefix"]
            match_forward_flow = False

        return self._add_chain_rule(
            chain, action='accept', properties=props,
            match_forward_flow=match_forward_flow,
            ip_addr_group_src=src_ipg_id, ip_addr_group_dst=dst_ipg_id,
            src_addr=src_addr, dst_addr=dst_addr,
            src_port_from=src_port_from, src_port_to=src_port_to,
            dst_port_from=dst_port_from, dst_port_to=dst_port_to,
            nw_proto=net_util.get_protocol_value(sg_rule["protocol"]),
            dl_type=net_util.get_ethertype_value(sg_rule["ethertype"]))

    def _remove_nat_rules(self, fip):
        router = self.client.get_router(fip["router_id"])
        self.client.remove_static_route(self._get_provider_router(),
                                        fip["floating_ip_address"])

        chain_names = _nat_chain_names(router.get_id())
        for _type, name in chain_names.iteritems():
            self.client.remove_rules_by_property(
                router.get_tenant_id(), name,
                OS_FLOATING_IP_RULE_KEY, fip["id"])

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
        """Create Neutron subnet.

        Creates a Neutron subnet and a DHCP entry in MidoNet bridge.
        """
        LOG.info(_("MidonetPluginV2.create_subnet called: subnet=%r"), subnet)

        s = subnet["subnet"]
        net = super(MidonetPluginV2, self).get_network(
            context, subnet['subnet']['network_id'], fields=None)

        session = context.session
        with session.begin(subtransactions=True):
            sn_entry = super(MidonetPluginV2, self).create_subnet(context,
                                                                  subnet)
            bridge = self.client.get_bridge(sn_entry['network_id'])

            self.client.create_dhcp(bridge, sn_entry['gateway_ip'],
                                    sn_entry['cidr'],
                                    host_rts=sn_entry['host_routes'],
                                    dns_servers=sn_entry['dns_nameservers'],
                                    enabled=sn_entry['enable_dhcp'])

            # For external network, link the bridge to the provider router.
            if net[ext_net.EXTERNAL] and s['gateway_ip']:
                self._link_to_provider_router(bridge, s['gateway_ip'],
                                              s['cidr'])

        LOG.info(_("MidonetPluginV2.create_subnet exiting: sn_entry=%r"),
                 sn_entry)
        return sn_entry

    def update_subnet(self, context, id, subnet):
        """Update the subnet with new info.
        """

        session = context.session
        with session.begin(subtransactions=True):
            s = super(MidonetPluginV2,
                      self).update_subnet(context, id, subnet)

            bridge = self.client.get_bridge(s['network_id'])
            self.client.update_dhcp(bridge, s['cidr'], s['gateway_ip'],
                                    host_rts=s['host_routes'],
                                    dns_servers=s['dns_nameservers'],
                                    enabled=s['enable_dhcp'])
        return s

    def delete_subnet(self, context, id):
        """Delete Neutron subnet.

        Delete neutron network and its corresponding MidoNet bridge.
        """
        LOG.info(_("MidonetPluginV2.delete_subnet called: id=%s"), id)
        subnet = super(MidonetPluginV2, self).get_subnet(context, id,
                                                         fields=None)
        net = super(MidonetPluginV2, self).get_network(context,
                                                       subnet['network_id'],
                                                       fields=None)
        session = context.session
        with session.begin(subtransactions=True):

            super(MidonetPluginV2, self).delete_subnet(context, id)
            bridge = self.client.get_bridge(subnet['network_id'])
            self.client.delete_dhcp(bridge, subnet['cidr'])

            # If the network is external, clean up routes, links, ports
            if net[ext_net.EXTERNAL]:
                self._unlink_from_provider_router(bridge, subnet)

            LOG.info(_("MidonetPluginV2.delete_subnet exiting"))

    def create_network(self, context, network):
        """Create Neutron network.

        Create a new Neutron network and its corresponding MidoNet bridge.
        """
        LOG.info(_('MidonetPluginV2.create_network called: network=%r'),
                 network)
        net_data = network['network']
        tenant_id = self._get_tenant_id_for_create(context, net_data)
        net_data['tenant_id'] = tenant_id
        self._ensure_default_security_group(context, tenant_id)

        bridge = self.client.create_bridge(**net_data)
        net_data['id'] = bridge.get_id()

        session = context.session
        with session.begin(subtransactions=True):
            net = super(MidonetPluginV2, self).create_network(context, network)
            self._process_l3_create(context, net, net_data)

        LOG.info(_("MidonetPluginV2.create_network exiting: net=%r"), net)
        return net

    def update_network(self, context, id, network):
        """Update Neutron network.

        Update an existing Neutron network and its corresponding MidoNet
        bridge.
        """
        LOG.info(_("MidonetPluginV2.update_network called: id=%(id)r, "
                   "network=%(network)r"), {'id': id, 'network': network})
        session = context.session
        with session.begin(subtransactions=True):
            net = super(MidonetPluginV2, self).update_network(
                context, id, network)
            self._process_l3_update(context, net, network['network'])
            self.client.update_bridge(id, **network['network'])

        LOG.info(_("MidonetPluginV2.update_network exiting: net=%r"), net)
        return net

    def get_network(self, context, id, fields=None):
        """Get Neutron network.

        Retrieves a Neutron network and its corresponding MidoNet bridge.
        """
        LOG.debug(_("MidonetPluginV2.get_network called: id=%(id)r, "
                    "fields=%(fields)r"), {'id': id, 'fields': fields})
        qnet = super(MidonetPluginV2, self).get_network(context, id, fields)
        self.client.get_bridge(id)

        LOG.debug(_("MidonetPluginV2.get_network exiting: qnet=%r"), qnet)
        return qnet

    def delete_network(self, context, id):
        """Delete a network and its corresponding MidoNet bridge."""
        LOG.info(_("MidonetPluginV2.delete_network called: id=%r"), id)
        net = super(MidonetPluginV2, self).get_network(context, id,
                                                       fields=None)

        try:
            super(MidonetPluginV2, self).delete_network(context, id)
        except Exception:
            LOG.error(_('Failed to delete neutron db, while Midonet bridge=%r'
                      'had been deleted'), id)
            raise

        # if the network is external, it may need to have its bridges
        # unplugged from the provider router.
        if net[ext_net.EXTERNAL]:
            # we currently only support one subnet in a network, so this
            # loop will only execute 0 or 1 times currently.
            for subnet_id in net['subnets']:
                subnet = self._get_subnet(context, subnet_id)
                bridge = self.client.get_bridge(id)
                self._unlink_from_provider_router(bridge, subnet)

        self.client.delete_bridge(id)

    @utils.synchronized('port-critical-section', external=True)
    def create_port(self, context, port):
        """Create a L2 port in Neutron/MidoNet."""
        LOG.info(_("MidonetPluginV2.create_port called: port=%r"), port)
        port_data = port['port']

        # Create a bridge port in MidoNet and set the bridge port ID as the
        # port ID in Neutron.
        bridge = self.client.get_bridge(port_data["network_id"])
        tenant_id = port_data['tenant_id']
        asu = port_data.get("admin_state_up", True)
        bridge_port = self.client.add_bridge_port(bridge,
                                                  admin_state_up=asu)
        port_data["id"] = bridge_port.get_id()

        try:
            session = context.session
            with session.begin(subtransactions=True):
                # Create a Neutron port
                new_port = super(MidonetPluginV2, self).create_port(context,
                                                                    port)
                port_data.update(new_port)
                self._ensure_default_security_group_on_port(context,
                                                            port)
                if _is_vif_port(port_data):
                    # Bind security groups to the port
                    sg_ids = self._get_security_groups_on_port(context, port)
                    self._bind_port_to_sgs(context, port_data, sg_ids)

                    # Create port chains
                    port_chains = {}
                    for d, name in _port_chain_names(
                            new_port["id"]).iteritems():
                        port_chains[d] = self.client.create_chain(tenant_id,
                                                                  name)

                    self._initialize_port_chains(context,
                                                 port_data,
                                                 port_chains['inbound'],
                                                 port_chains['outbound'],
                                                 sg_ids)

                    # Update the port with the chain
                    self.client.update_port_chains(
                        bridge_port, port_chains["inbound"].get_id(),
                        port_chains["outbound"].get_id())

                    # DHCP mapping is only for VIF ports
                    for cidr, ip, mac in self._dhcp_mappings(
                            context, port_data["fixed_ips"],
                            port_data["mac_address"]):
                        self.client.add_dhcp_host(bridge, cidr, ip, mac)

                    net = super(MidonetPluginV2,
                                self).get_network(context,
                                                  port_data['network_id'],
                                                  fields=None)

                    # if the network is an external network, then each port
                    # created on this network needs a route on the provider
                    # router to override the general 'blackhole' route that
                    # is set up to drop all illegitimate traffic to this
                    # network.
                    if net[ext_net.EXTERNAL]:
                        for ip in port_data["fixed_ips"]:
                            subnet = self._get_subnet(context, ip["subnet_id"])
                            if subnet["ip_version"] == 6:
                                continue
                            self._add_route_to_provider(bridge,
                                                        ip["ip_address"])

                elif _is_dhcp_port(port_data):
                    # For DHCP port, add a metadata route
                    for cidr, ip in self._metadata_subnets(
                            context, port_data["fixed_ips"]):
                        self.client.add_dhcp_route_option(bridge, cidr, ip,
                                                          METADATA_DEFAULT_IP)

        except Exception as ex:
            # Try removing the MidoNet port before raising an exception.
            with excutils.save_and_reraise_exception():
                LOG.error(_("Failed to create a port on network %(net_id)s: "
                            "%(err)s"),
                          {"net_id": port_data["network_id"], "err": ex})
                self.client.delete_port(bridge_port.get_id())

        LOG.info(_("MidonetPluginV2.create_port exiting: port=%r"), port_data)
        return port_data

    def get_port(self, context, id, fields=None):
        """Retrieve port."""
        LOG.debug(_("MidonetPluginV2.get_port called: id=%(id)s "
                    "fields=%(fields)r"), {'id': id, 'fields': fields})
        port = super(MidonetPluginV2, self).get_port(context, id, fields)
        "Check if the port exists in MidoNet DB"""
        try:
            self.client.get_port(id)
        except midonet_lib.MidonetResourceNotFound as exc:
            LOG.error(_("There is no port with ID %(id)s in MidoNet."),
                      {"id": id})
            port['status'] = constants.PORT_STATUS_ERROR
            raise exc
        LOG.debug(_("MidonetPluginV2.get_port exiting: port=%r"), port)
        return port

    def get_ports(self, context, filters=None, fields=None):
        """List neutron ports and verify that they exist in MidoNet."""
        LOG.debug(_("MidonetPluginV2.get_ports called: filters=%(filters)s "
                    "fields=%(fields)r"),
                  {'filters': filters, 'fields': fields})
        ports = super(MidonetPluginV2, self).get_ports(context, filters,
                                                       fields)
        return ports

    @utils.synchronized('port-critical-section', external=True)
    def delete_port(self, context, id, l3_port_check=True):
        """Delete a neutron port and corresponding MidoNet bridge port."""
        LOG.info(_("MidonetPluginV2.delete_port called: id=%(id)s "
                   "l3_port_check=%(l3_port_check)r"),
                 {'id': id, 'l3_port_check': l3_port_check})
        # if needed, check to see if this is a port owned by
        # and l3-router.  If so, we should prevent deletion.
        if l3_port_check:
            self.prevent_l3_port_deletion(context, id)

        self.disassociate_floatingips(context, id)
        port = self.get_port(context, id)
        device_id = port['device_id']
        # If this port is for router interface/gw, unlink and delete.
        if _is_router_interface_port(port):
            self._unlink_bridge_from_router(device_id, id)
        elif _is_router_gw_port(port):
            # Gateway removed
            # Remove all the SNAT rules that are tagged.
            router = self._get_router(context, device_id)
            tenant_id = router["tenant_id"]
            chain_names = _nat_chain_names(device_id)
            for _type, name in chain_names.iteritems():
                self.client.remove_rules_by_property(
                    tenant_id, name, OS_TENANT_ROUTER_RULE_KEY,
                    SNAT_RULE)
            # Remove the default routes and unlink
            self._remove_router_gateway(port['device_id'])
        elif _is_vif_port(port):
            net = super(MidonetPluginV2,
                        self).get_network(context,
                                          port['network_id'],
                                          fields=None)
            if net[ext_net.EXTERNAL]:
                for ip in port["fixed_ips"]:
                    self._remove_route_from_provider(ip["ip_address"])

        self.client.delete_port(id, delete_chains=True)
        try:
            for cidr, ip, mac in self._dhcp_mappings(
                    context, port["fixed_ips"], port["mac_address"]):
                self.client.delete_dhcp_host(port["network_id"], cidr, ip,
                                             mac)
        except Exception:
            LOG.error(_("Failed to delete DHCP mapping for port %(id)s"),
                      {"id": id})

        super(MidonetPluginV2, self).delete_port(context, id)

    @utils.synchronized('port-critical-section', external=True)
    def update_port(self, context, id, port):
        """Handle port update, including security groups and fixed IPs."""
        with context.session.begin(subtransactions=True):

            # Get the port and save the fixed IPs
            old_port = self._get_port(context, id)
            net_id = old_port["network_id"]
            mac = old_port["mac_address"]
            old_ips = old_port["fixed_ips"]
            # update the port DB
            p = super(MidonetPluginV2, self).update_port(context, id, port)

            if "admin_state_up" in port["port"]:
                asu = port["port"]["admin_state_up"]
                mido_port = self.client.update_port(id, admin_state_up=asu)

                # If we're changing the admin_state_up flag and the port is
                # associated with a router, then we also need to update the
                # peer port.
                if _is_router_interface_port(p):
                    self.client.update_port(mido_port.get_peer_id(),
                                            admin_state_up=asu)

            new_ips = p["fixed_ips"]
            if new_ips:
                bridge = self.client.get_bridge(net_id)
                # If it's a DHCP port, add a route to reach the MD server
                if _is_dhcp_port(p):
                    for cidr, ip in self._metadata_subnets(context, new_ips):
                        self.client.add_dhcp_route_option(
                            bridge, cidr, ip, METADATA_DEFAULT_IP)
                else:
                # IPs have changed.  Re-map the DHCP entries
                    for cidr, ip, mac in self._dhcp_mappings(
                            context, old_ips, mac):
                        self.client.remove_dhcp_host(
                            bridge, cidr, ip, mac)

                    for cidr, ip, mac in self._dhcp_mappings(context, new_ips,
                                                             mac):
                        self.client.add_dhcp_host(
                            bridge, cidr, ip, mac)

            if (self._check_update_deletes_security_groups(port) or
                    self._check_update_has_security_groups(port)):
                self._unbind_port_from_sgs(context, p)
                new_sg_ids = self._get_security_groups_on_port(context, port)
                self._bind_port_to_sgs(context, p, new_sg_ids)

        return p

    @_enable_snat_col_if_missing
    def get_routers_count(self, context, filters=None):
        return super(MidonetPluginV2, self).get_routers_count(context, filters)

    @_enable_snat_col_if_missing
    def get_routers(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        return super(MidonetPluginV2, self).get_routers(context, filters,
                                                        fields, sorts, limit,
                                                        marker, page_reverse)

    def create_router(self, context, router):
        """Handle router creation.

        When a new Neutron router is created, its corresponding MidoNet router
        is also created.  In MidoNet, this router is initialized with chains
        for inbound and outbound traffic, which will be used to hold other
        chains that include various rules, such as NAT.

        :param router: Router information provided to create a new router.
        """

        # NOTE(dcahill): Similar to the Nicira plugin, we completely override
        # this method in order to be able to use the MidoNet ID as Neutron ID
        # TODO(dcahill): Propose upstream patch for allowing
        # 3rd parties to specify IDs as we do with l2 plugin
        LOG.info(_("MidonetPluginV2.create_router called: router=%(router)s"),
                 {"router": router})
        r = router['router']
        tenant_id = self._get_tenant_id_for_create(context, r)
        r['tenant_id'] = tenant_id
        mido_router = self.client.create_router(**r)
        mido_router_id = mido_router.get_id()

        # Create router chains
        chain_names = _nat_chain_names(mido_router_id)
        try:
            self.client.add_router_chains(mido_router,
                                          chain_names["pre-routing"],
                                          chain_names["post-routing"])
        except Exception:
            # Set the router status to Error
            with context.session.begin(subtransactions=True):
                r = self._get_router(context, mido_router_id)
                r['status'] = constants.NET_STATUS_ERROR
                context.session.add(r)
            return r

        try:
            has_gw_info = False
            if EXTERNAL_GW_INFO in r and r[EXTERNAL_GW_INFO]:
                has_gw_info = True
                gw_info = r.pop(EXTERNAL_GW_INFO)
            with context.session.begin(subtransactions=True):
                # pre-generate id so it will be available when
                # configuring external gw port
                router_db = l3_db.Router(id=mido_router_id,
                                         tenant_id=tenant_id,
                                         name=r['name'],
                                         admin_state_up=r['admin_state_up'],
                                         status="ACTIVE")
                context.session.add(router_db)
                if has_gw_info:
                    self._update_router_gw_info(context, router_db['id'],
                                                gw_info)
                    router_data = self._make_router_dict(router_db)
                    self._set_up_gateway(context, router_data)
                else:
                    router_data = self._make_router_dict(router_db)

        except Exception:
            # Try removing the midonet router
            with excutils.save_and_reraise_exception():
                self.client.delete_router(mido_router_id)

        LOG.info(_("MidonetPluginV2.create_router exiting: "
                   "router_data=%(router_data)s."),
                 {"router_data": router_data})
        return router_data

    def _set_router_gateway(self, id, gw_router, gw_ip):
        """Set router uplink gateway

        :param ID: ID of the router
        :param gw_router: gateway router to link to
        :param gw_ip: gateway IP address
        """
        LOG.info(_("MidonetPluginV2.set_router_gateway called: id=%(id)s, "
                   "gw_router=%(gw_router)s, gw_ip=%(gw_ip)s"),
                 {'id': id, 'gw_router': gw_router, 'gw_ip': gw_ip}),

        router = self.client.get_router(id)

        # Create a port in the gw router
        gw_port = self.client.add_router_port(gw_router,
                                              port_address='169.254.255.1',
                                              network_address='169.254.255.0',
                                              network_length=30)

        # Create a port in the router
        port = self.client.add_router_port(router,
                                           port_address='169.254.255.2',
                                           network_address='169.254.255.0',
                                           network_length=30)

        # Link them
        self.client.link(gw_port, port.get_id())

        # Add a route for gw_ip to bring it down to the router
        self.client.add_router_route(gw_router, type='Normal',
                                     src_network_addr='0.0.0.0',
                                     src_network_length=0,
                                     dst_network_addr=gw_ip,
                                     dst_network_length=32,
                                     next_hop_port=gw_port.get_id(),
                                     weight=100)

        # Add default route to uplink in the router
        self.client.add_router_route(router, type='Normal',
                                     src_network_addr='0.0.0.0',
                                     src_network_length=0,
                                     dst_network_addr='0.0.0.0',
                                     dst_network_length=0,
                                     next_hop_port=port.get_id(),
                                     weight=100)

    def _remove_router_gateway(self, id):
        """Clear router gateway

        :param ID: ID of the router
        """
        LOG.info(_("MidonetPluginV2.remove_router_gateway called: "
                   "id=%(id)s"), {'id': id})
        router = self.client.get_router(id)

        # delete the port that is connected to the gateway router
        for p in router.get_ports():
            if p.get_port_address() == '169.254.255.2':
                peer_port_id = p.get_peer_id()
                if peer_port_id is not None:
                    self.client.unlink(p)
                    self.client.delete_port(peer_port_id)

        # delete default route
        for r in router.get_routes():
            if (r.get_dst_network_addr() == '0.0.0.0' and
                    r.get_dst_network_length() == 0):
                self.client.delete_route(r.get_id())

    def _set_up_gateway(self, context, router_db):
        # Gateway created
        tenant_id = router_db["tenant_id"]
        gw_port_neutron = self._get_port(context.elevated(),
                                         router_db["gw_port_id"])
        gw_ip = gw_port_neutron['fixed_ips'][0]['ip_address']

        # First link routers and set up the routes
        self._set_router_gateway(router_db["id"],
                                 self._get_provider_router(),
                                 gw_ip)
        gw_port_midonet = self.client.get_link_port(
            self._get_provider_router(), router_db["id"])

        # Get the NAT chains and add dynamic SNAT rules.
        gw_info = router_db[EXTERNAL_GW_INFO]
        if gw_info.get('enable_snat', True):
            chain_names = _nat_chain_names(router_db["id"])
            props = {OS_TENANT_ROUTER_RULE_KEY: SNAT_RULE}
            self.client.add_dynamic_snat(tenant_id,
                                         chain_names['pre-routing'],
                                         chain_names['post-routing'],
                                         gw_ip, gw_port_midonet.get_id(),
                                         **props)

    def update_router(self, context, id, router):
        """Handle router updates."""
        LOG.info(_("MidonetPluginV2.update_router called: id=%(id)s "
                   "router=%(router)r"), {"id": id, "router": router})

        router_data = router["router"]

        # Check if the update included changes to the gateway.
        gw_updated = l3_db.EXTERNAL_GW_INFO in router_data
        with context.session.begin(subtransactions=True):

            # Update the Neutron DB
            r = super(MidonetPluginV2, self).update_router(context, id,
                                                           router)

            if (gw_updated and l3_db.EXTERNAL_GW_INFO in r and
                    r[l3_db.EXTERNAL_GW_INFO] is not None):
                self._set_up_gateway(context, r)

            self.client.update_router(id, **router_data)

        LOG.info(_("MidonetPluginV2.update_router exiting: router=%r"), r)
        return r

    def delete_router(self, context, id):
        """Handler for router deletion.

        Deleting a router on Neutron simply means deleting its corresponding
        router in MidoNet.

        :param id: router ID to remove
        """
        LOG.info(_("MidonetPluginV2.delete_router called: id=%s"), id)
        super(MidonetPluginV2, self).delete_router(context, id)

        self.client.delete_router_chains(id)
        self.client.delete_router(id)

    def _add_route_to_provider(self, bridge, ip):
        """Add a route to the given IP through the given bridge.

        :param bridge: bridge that is hooked up to the provider router.
        :param ip: ip that will have traffic routed through the
                   provider router.
        """
        provider_router = self._get_provider_router()
        link_port = self.client.get_link_port(provider_router, bridge.get_id())
        self.client.add_router_route(provider_router, type='Normal',
                                     src_network_addr='0.0.0.0',
                                     src_network_length=0,
                                     dst_network_addr=ip,
                                     dst_network_length=32,
                                     next_hop_port=link_port.get_peer_id(),
                                     weight=100)

    def _remove_route_from_provider(self, ip):
        provider_router = self._get_provider_router()
        for route in provider_router.get_routes():
            if (route.get_dst_network_addr() == ip and
                    route.get_dst_network_length() == 32):
                self.client.delete_route(route.get_id())

    def _link_to_provider_router(self, bridge, gw_ip, cidr):
        """Link a bridge to the provider router

        :param bridge:  bridge
        :param gw_ip: IP address of gateway
        :param cidr: network CIDR
        """
        provider_router = self._get_provider_router()
        net_addr, net_len = net_util.net_addr(cidr)

        # create a port on the gateway router
        gw_port = self.client.add_router_port(provider_router,
                                              port_address=gw_ip,
                                              network_address=net_addr,
                                              network_length=net_len)

        # create a bridge port, then link it to the router.
        port = self.client.add_bridge_port(bridge)
        self.client.link(gw_port, port.get_id())

        # add a route for the subnet in the gateway router. We 'BlackHole' all
        # of the traffic to this subnet because legitimate targets will have
        # a more specific route. We drop illegitimate traffic for performance.
        self.client.add_router_route(provider_router, type='BlackHole',
                                     src_network_addr='0.0.0.0',
                                     src_network_length=0,
                                     dst_network_addr=net_addr,
                                     dst_network_length=net_len,
                                     weight=100)

    def _unlink_from_provider_router(self, bridge, subnet):
        """Unlink a bridge from the provider router

        :param bridge: bridge to unlink
        """

        provider_router = self._get_provider_router()
        # Delete routes and unlink the router and the bridge.
        routes = self.client.get_router_routes(provider_router.get_id())
        subnet_routes = self.client.filter_routes(routes, cidr=subnet['cidr'])
        # delete routes on the provider router that point to this subnet
        for r in subnet_routes:
            self.client.delete_route(r.get_id())

        bridge_ports_to_delete = [
            p for p in provider_router.get_peer_ports()
            if p.get_device_id() == bridge.get_id()]

        for p in bridge.get_peer_ports():
            if p.get_device_id() == provider_router.get_id():
                # delete the routes using this subnet as the next hop
                filtered_routes = self.client.filter_routes(
                    routes, port_id=p.get_id())
                for r in filtered_routes:
                    self.client.delete_route(r.get_id())
                self.client.unlink(p)
                self.client.delete_port(p.get_id())

        # delete bridge port
        for port in bridge_ports_to_delete:
            self.client.delete_port(port.get_id())

    def _link_bridge_to_router(self, router, bridge_port, net_addr, net_len,
                               gw_ip, metadata_gw_ip):
        router_port = self.client.add_router_port(
            router, network_length=net_len, network_address=net_addr,
            port_address=gw_ip, admin_state_up=bridge_port['admin_state_up'])
        self.client.link(router_port, bridge_port['id'])
        self.client.add_router_route(router, type='Normal',
                                     src_network_addr='0.0.0.0',
                                     src_network_length=0,
                                     dst_network_addr=net_addr,
                                     dst_network_length=net_len,
                                     next_hop_port=router_port.get_id(),
                                     weight=100)

        if metadata_gw_ip:
            # Add a route for the metadata server.
            # Not all VM images supports DHCP option 121.  Add a route for the
            # Metadata server in the router to forward the packet to the bridge
            # that will send them to the Metadata Proxy.
            md_net_addr, md_net_len = net_util.net_addr(METADATA_DEFAULT_IP)
            self.client.add_router_route(
                router, type='Normal', src_network_addr=net_addr,
                src_network_length=net_len,
                dst_network_addr=md_net_addr,
                dst_network_length=md_net_len,
                next_hop_port=router_port.get_id(),
                next_hop_gateway=metadata_gw_ip)

    def _unlink_bridge_from_router(self, router_id, bridge_port_id):
        """Unlink a bridge from a router."""

        # Remove the routes to the port and unlink the port
        bridge_port = self.client.get_port(bridge_port_id)
        routes = self.client.get_router_routes(router_id)
        self.client.delete_port_routes(routes, bridge_port.get_peer_id())
        self.client.unlink(bridge_port)

    # Use as a decorator to retry
    def retryloop(attempts, delay):
        def internal_wrapper(func):
            def retry(*args, **kwargs):
                for i in range(attempts):
                    result = func(*args, **kwargs)
                    if result is None:
                        time.sleep(delay)
                    else:
                        return result
                return None
            return retry
        return internal_wrapper

    # retry 5 times, sleeping for one second each time. In general, this will
    # pass on the first try. There is a corner case when it doesn't: if this
    # is called directly after the subnet has been created. There is a small
    # window where the dhcp port is in the process of being created.
    @retryloop(5, 1)
    def _get_dhcp_port_ip(self, context, subnet):
        rport_qry = context.session.query(models_v2.Port)
        dhcp_ports = rport_qry.filter_by(
            network_id=subnet["network_id"],
            device_owner='network:dhcp').all()
        if dhcp_ports and dhcp_ports[0].fixed_ips:
            return dhcp_ports[0].fixed_ips[0].ip_address
        else:
            return None

    def add_router_interface(self, context, router_id, interface_info):
        """Handle router linking with network."""
        LOG.info(_("MidonetPluginV2.add_router_interface called: "
                   "router_id=%(router_id)s "
                   "interface_info=%(interface_info)r"),
                 {'router_id': router_id, 'interface_info': interface_info})

        with context.session.begin(subtransactions=True):
            info = super(MidonetPluginV2, self).add_router_interface(
                context, router_id, interface_info)

        try:
            subnet = self._get_subnet(context, info["subnet_id"])
            cidr = subnet["cidr"]
            net_addr, net_len = net_util.net_addr(cidr)
            router = self.client.get_router(router_id)

            # Get the metadata GW IP
            metadata_gw_ip = self._get_dhcp_port_ip(context, subnet)
            if metadata_gw_ip is None:
                LOG.warn(_("The DHCP port does not yet exist. This is "
                           "possibly a race condition where the port is "
                           "currently being created. Currently no port "
                           "to set up DHCP route."))

            # Link the router and the bridge
            port = super(MidonetPluginV2, self).get_port(context,
                                                         info["port_id"])
            self._link_bridge_to_router(router, port, net_addr,
                                        net_len, subnet["gateway_ip"],
                                        metadata_gw_ip)
        except Exception:
            LOG.error(_("Failed to create MidoNet resources to add router "
                        "interface. info=%(info)s, router_id=%(router_id)s"),
                      {"info": info, "router_id": router_id})
            with excutils.save_and_reraise_exception():
                with context.session.begin(subtransactions=True):
                    self.remove_router_interface(context, router_id, info)

        LOG.info(_("MidonetPluginV2.add_router_interface exiting: "
                   "info=%r"), info)
        return info

    def _assoc_fip(self, fip):
        router = self.client.get_router(fip["router_id"])
        self._add_route_to_provider(router, fip['floating_ip_address'])
        link_port = self.client.get_link_port(self._get_provider_router(),
                                              router.get_id())
        self._add_nat_rules(router, link_port, fip)

    def _add_nat_rules(self, router, link_port, fip):
        router = self.client.get_router(fip["router_id"])
        props = {OS_FLOATING_IP_RULE_KEY: fip['id']}
        tenant_id = router.get_tenant_id()
        chain_names = _nat_chain_names(router.get_id())
        for chain_type, name in chain_names.items():
            src_ip, target_ip = _get_nat_ips(chain_type, fip)
            if chain_type == 'pre-routing':
                nat_type = 'dnat'
            else:
                nat_type = 'snat'
            self.client.add_static_nat(tenant_id, name, src_ip,
                                       target_ip,
                                       link_port.get_id(),
                                       nat_type, **props)

    def _disassoc_fip(self, fip):
        self._remove_route_from_provider(fip["floating_ip_address"])
        self._remove_nat_rules(fip)

    def create_floatingip(self, context, floatingip):
        session = context.session
        with session.begin(subtransactions=True):
            fip = super(MidonetPluginV2, self).create_floatingip(
                context, floatingip)
            if fip['port_id']:
                self._assoc_fip(fip)
        return fip

    def update_floatingip(self, context, id, floatingip):
        """Handle floating IP assocation and disassociation."""
        LOG.info(_("MidonetPluginV2.update_floatingip called: id=%(id)s "
                   "floatingip=%(floatingip)s "),
                 {'id': id, 'floatingip': floatingip})

        session = context.session
        with session.begin(subtransactions=True):
            old_fip = super(MidonetPluginV2, self).get_floatingip(context, id)
            new_fip = super(MidonetPluginV2,
                            self).update_floatingip(context, id, floatingip)

            old_fip_port = old_fip['port_id']
            new_fip_port = new_fip['port_id']

            # getting rid of the port association
            if old_fip_port and not new_fip_port:
                self._disassoc_fip(old_fip)
            # adding a port association
            elif not old_fip_port and new_fip_port:
                self._assoc_fip(new_fip)
            # changing the port association
            elif old_fip_port and new_fip_port:
                if old_fip_port != new_fip_port:
                    self._disassoc_fip(old_fip)
                    self._assoc_fip(new_fip)

        LOG.info(_("MidonetPluginV2.update_floating_ip exiting: new_fip=%s"),
                 new_fip)
        return new_fip

    def disassociate_floatingips(self, context, port_id):
        """Disassociate floating IPs (if any) from this port."""
        try:
            fip_qry = context.session.query(l3_db.FloatingIP)
            fip_db = fip_qry.filter_by(fixed_port_id=port_id).one()
            if fip_db and fip_db['fixed_port_id']:
                self._disassoc_fip(fip_db)
        except sao_exc.NoResultFound:
            pass

        super(MidonetPluginV2, self).disassociate_floatingips(context, port_id)

    def delete_floatingip(self, context, id):
        floatingip = super(MidonetPluginV2, self).get_floatingip(context, id)
        if floatingip['port_id']:
            self._disassoc_fip(floatingip)
        super(MidonetPluginV2, self).delete_floatingip(context, id)

    def create_security_group(self, context, security_group, default_sg=False):
        """Create security group.

        Create a new security group, including the default security group.
        In MidoNet, this means creating a pair of chains, inbound and outbound,
        as well as a new IP address group.
        """
        LOG.info(_("MidonetPluginV2.create_security_group called: "
                   "security_group=%(security_group)s "
                   "default_sg=%(default_sg)s "),
                 {'security_group': security_group, 'default_sg': default_sg})

        sg = security_group.get('security_group')
        tenant_id = self._get_tenant_id_for_create(context, sg)
        if not default_sg:
            self._ensure_default_security_group(context, tenant_id)

        # Create the Neutron sg first
        sg = super(MidonetPluginV2, self).create_security_group(
            context, security_group, default_sg)

        try:
            # Process the MidoNet side
            sg_id = sg["id"]
            self.client.create_ip_addr_group(sg_id,
                                             _sg_ip_addr_group_name(sg_id))
            chain_names = _sg_chain_names(sg["id"])
            chains = {}
            for direction, chain_name in chain_names.iteritems():
                c = self.client.create_chain(tenant_id, chain_name)
                chains[direction] = c

            # Create all the rules for this SG.  Only accept rules are created
            for r in sg['security_group_rules']:
                self._create_accept_chain_rule(context, r,
                                               chain=chains[r['direction']])
        except Exception:
            LOG.error(_("Failed to create MidoNet resources for sg %(sg)r"),
                      {"sg": sg})
            with excutils.save_and_reraise_exception():
                with context.session.begin(subtransactions=True):
                    sg = self._get_security_group(context, sg["id"])
                    context.session.delete(sg)

        LOG.info(_("MidonetPluginV2.create_security_group exiting: sg=%r"),
                 sg)
        return sg

    def delete_security_group(self, context, id):
        """Delete chains for Neutron security group."""
        LOG.info(_("MidonetPluginV2.delete_security_group called: id=%s"), id)

        with context.session.begin(subtransactions=True):
            sg = super(MidonetPluginV2, self).get_security_group(context, id)
            if not sg:
                raise ext_sg.SecurityGroupNotFound(id=id)

            if sg["name"] == 'default' and not context.is_admin:
                raise ext_sg.SecurityGroupCannotRemoveDefault()

            sg_id = sg['id']
            filters = {'security_group_id': [sg_id]}
            if super(MidonetPluginV2, self)._get_port_security_group_bindings(
                    context, filters):
                raise ext_sg.SecurityGroupInUse(id=sg_id)

            # Delete MidoNet Chains and IP address group for the SG
            tenant_id = sg['tenant_id']
            self.client.delete_chains_by_names(
                tenant_id, _sg_chain_names(sg["id"]).values())

            self.client.delete_ip_addr_group(sg["id"])

            super(MidonetPluginV2, self).delete_security_group(context, id)

    def create_security_group_rule(self, context, security_group_rule):
        """Create a single security group rule."""
        bulk_rule = {'security_group_rules': [security_group_rule]}
        return self.create_security_group_rule_bulk(context, bulk_rule)[0]

    def create_security_group_rule_bulk(self, context, security_group_rule):
        """Create multiple security group rules

        Create multiple security group rules in the Neutron DB and
        corresponding MidoNet resources in its data store.
        """
        LOG.info(_("MidonetPluginV2.create_security_group_rule_bulk called: "
                   "security_group_rule=%(security_group_rule)r"),
                 {'security_group_rule': security_group_rule})

        with context.session.begin(subtransactions=True):
            rules = super(
                MidonetPluginV2, self).create_security_group_rule_bulk_native(
                    context, security_group_rule)
            i = 0
            for rule in rules:
                try:
                    i += 1
                    self._create_accept_chain_rule(context, rule)
                    for j in xrange(0, i):
                        self.client.remove_chain_rule(rules[j]['id'])
                except Exception:
                    LOG.error(_("Failed to create MidoNet rule  %(rule)r"),
                              {"rule": rule})
                    for j in xrange(0, i):
                        self.client.remove_chain_rule(rules[j]['id'])
                    raise

            LOG.info(_("MidonetPluginV2.create_security_group_rule_bulk "
                       "exiting: rules=%r"), rules)
            return rules

    def delete_security_group_rule(self, context, sg_rule_id):
        """Delete a security group rule

        Delete a security group rule from the Neutron DB and corresponding
        MidoNet resources from its data store.
        """
        LOG.info(_("MidonetPluginV2.delete_security_group_rule called: "
                   "sg_rule_id=%s"), sg_rule_id)
        with context.session.begin(subtransactions=True):
            rule = super(MidonetPluginV2, self).get_security_group_rule(
                context, sg_rule_id)

            if not rule:
                raise ext_sg.SecurityGroupRuleNotFound(id=sg_rule_id)

            sg = self._get_security_group(context,
                                          rule["security_group_id"])
            chain_name = _sg_chain_names(sg["id"])[rule["direction"]]
            self.client.remove_rules_by_property(rule["tenant_id"], chain_name,
                                                 OS_SG_RULE_KEY,
                                                 str(rule["id"]))
            super(MidonetPluginV2, self).delete_security_group_rule(
                context, sg_rule_id)

    def _add_chain_rule(self, chain, action, **kwargs):

        nw_proto = kwargs.get("nw_proto")
        src_addr = kwargs.pop("src_addr", None)
        dst_addr = kwargs.pop("dst_addr", None)
        src_port_from = kwargs.pop("src_port_from", None)
        src_port_to = kwargs.pop("src_port_to", None)
        dst_port_from = kwargs.pop("dst_port_from", None)
        dst_port_to = kwargs.pop("dst_port_to", None)

        # Convert to the keys and values that midonet client understands
        if src_addr:
            kwargs["nw_src_addr"], kwargs["nw_src_length"] = net_util.net_addr(
                src_addr)

        if dst_addr:
            kwargs["nw_dst_addr"], kwargs["nw_dst_length"] = net_util.net_addr(
                dst_addr)

        kwargs["tp_src"] = {"start": src_port_from, "end": src_port_to}

        kwargs["tp_dst"] = {"start": dst_port_from, "end": dst_port_to}

        if nw_proto == 1:  # ICMP
            # Overwrite port fields regardless of the direction
            kwargs["tp_src"] = {"start": src_port_from, "end": src_port_from}
            kwargs["tp_dst"] = {"start": dst_port_to, "end": dst_port_to}

        return self.client.add_chain_rule(chain, action=action, **kwargs)
